import os
import io
import logging
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User, Photo, Like, Comment, Save
from textblob import TextBlob
from PIL import Image, ImageStat
from dotenv import load_dotenv
import urllib.parse


# --- AZURE STORAGE LIBRARY ---
from azure.storage.blob import BlobServiceClient

# .env file se variables load karein
load_dotenv()

# --- LOGGING CONFIGURATION ---
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('photo_share_app')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'mysupersecretkeyIsVeryLongAndSecure')

# --- DATABASE CONFIGURATION (Azure PostgreSQL Persistence Fix) ---
# Docker redeploy par data bachane ke liye Azure PostgreSQL connection string lazmi hai
raw_conn = os.getenv('AZURE_POSTGRESQL_CONNECTIONSTRING') or os.getenv('DATABASE_URL')

def _mask_db_uri(uri: str) -> str:
    try:
        if uri.startswith('sqlite'): return uri
        parts = uri.split('://', 1)
        scheme = parts[0]
        rest = parts[1]
        if '@' in rest:
            userinfo, hostpath = rest.split('@', 1)
            user = userinfo.split(':', 1)[0] if ':' in userinfo else userinfo
            return f"{scheme}://{user}:****@{hostpath}"
        return uri
    except Exception: return "****"

SQLALCHEMY_DATABASE_URI = None

if raw_conn:
    if '://' in raw_conn:
        SQLALCHEMY_DATABASE_URI = raw_conn
    else:
        try:
            # Azure-style Key-Value parsing logic taake SQLalchemy connect ho sakay
            conn_params = dict(pair.split('=', 1) for pair in raw_conn.split())
            user = conn_params.get('user') or conn_params.get('username')
            password = urllib.parse.quote_plus(conn_params.get('password', ''))
            host = conn_params.get('host', 'localhost')
            port = conn_params.get('port', '5432')
            dbname = conn_params.get('dbname') or conn_params.get('database')
            SQLALCHEMY_DATABASE_URI = f"postgresql://{user}:{password}@{host}:{port}/{dbname}?sslmode=require"
        except Exception as e:
            logger.warning("DB Parsing failed; using raw value: %s", e)
            SQLALCHEMY_DATABASE_URI = raw_conn
    logger.info('✅ Using Persistent Azure Database: %s', _mask_db_uri(SQLALCHEMY_DATABASE_URI))
else:
    # Fallback to SQLite (Data will be deleted on redeploy)
    os.makedirs(os.path.join(app.root_path, 'instance'), exist_ok=True)
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(app.root_path, 'instance', 'app.db')}"
    logger.warning('⚠️ No Azure DB found. Using SQLite. DATA WILL BE LOST ON REDEPLOY.')

app.config['SQLALCHEMY_DATABASE_URI'] = SQLALCHEMY_DATABASE_URI
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- AZURE BLOB STORAGE CONFIGURATION ---
AZURE_CONNECTION_STRING = os.getenv('AZURE_STORAGE_CONNECTION_STRING')
AZURE_CONTAINER_NAME = os.getenv('AZURE_CONTAINER_NAME', 'photos')

# Folder for local fallback (if needed)
LOCAL_UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(LOCAL_UPLOAD_FOLDER, exist_ok=True)

blob_service_client = None
if AZURE_CONNECTION_STRING:
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
        logger.info('Azure Blob Storage initialized successfully.')
    except Exception as e:
        logger.error('Azure Storage Error: %s', e)

# Database Initialize
db.init_app(app)

# Auto-create tables on startup
with app.app_context():
    try:
        db.create_all()
        logger.info('Database tables checked/created.')
    except Exception as e:
        logger.exception('Critical: DB Connection Failed. Check Firewall!')

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- TEMPLATE FILTERS ---
@app.template_filter('timeago')
def timeago(date):
    if not date: 
        return "Recently"  # Crash fix: Agar date NULL ho to crash nahi karega
    diff = datetime.utcnow() - date
    s = diff.total_seconds()
    if s < 60: return "Just now"
    if s < 3600: return f"{int(s//60)}m ago"
    if s < 86400: return f"{int(s//3600)}h ago"
    return f"{int(s//86400)}d ago"

# --- DETAILED AI IMAGE ANALYSIS ---
def analyze_image(img_obj):
    tags = []
    try:
        if img_obj.mode != 'RGB': img_obj = img_obj.convert('RGB')
        
        # 1. Quality Analysis
        width, height = img_obj.size
        tags.append("HD ᴴᴰ" if width * height > 1000000 else "SD")

        # 2. Brightness Analysis
        stat = ImageStat.Stat(img_obj.convert('L'))
        brightness = stat.mean[0]
        if brightness > 150: tags.append("Bright ☀️")
        elif brightness < 80: tags.append("Dark 🌙")
        else: tags.append("Neutral Lighting ☁️")

        # 3. Color Analysis (Advanced Tone Detection)
        img_small = img_obj.resize((1, 1))
        r, g, b = img_small.getpixel((0, 0))
        if r > g and r > b: tags.append("Warm Tone 🔴")
        elif b > r and b > g: tags.append("Cool Tone 🔵")
        else: tags.append("Balanced Color 🎨")

    except Exception as e:
        logger.error(f"AI Analysis Error: {e}")
        return "Standard Photo"
    return " | ".join(tags)

# --- ROUTES ---

@app.route('/')
def home():
    return redirect(url_for('feed')) if current_user.is_authenticated else redirect(url_for('login'))

@app.route('/feed')
@login_required
def feed():
    query = request.args.get('q')
    if query:
        search_term = f"%{query}%"
        photos = Photo.query.join(User).filter(
            (Photo.title.ilike(search_term)) | (Photo.caption.ilike(search_term)) | 
            (Photo.location.ilike(search_term)) | (User.username.ilike(search_term))
        ).order_by(Photo.uploaded_at.desc()).all()
    else:
        photos = Photo.query.order_by(Photo.uploaded_at.desc()).all()
    return render_template('feed.html', photos=photos)

@app.route('/u/<username>')
@login_required
def profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    photos = Photo.query.filter_by(user_id=user.id).order_by(Photo.uploaded_at.desc()).all()
    saved = Photo.query.join(Save).filter(Save.user_id == user.id).all()
    liked = Photo.query.join(Like).filter(Like.user_id == user.id).all()
    return render_template('profile.html', user=user, photos=photos, saved_photos=saved, liked_photos=liked)

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def creator_dashboard():
    if current_user.role != 'creator':
        flash("Only Creators can upload photos.", 'warning')
        return redirect(url_for('feed'))
    if request.method == 'POST':
        file = request.files.get('photo')
        if file:
            filename = secure_filename(file.filename)
            try:
                img = Image.open(file)
                if img.mode != 'RGB': img = img.convert('RGB')
                auto_tags = analyze_image(img)
                img.thumbnail((1080, 1080))

                # Cloud upload if configured
                if blob_service_client and AZURE_CONTAINER_NAME:
                    logger.info('Uploading photo to Azure for user %s', current_user.username)
                    buf = io.BytesIO()
                    img.save(buf, format='JPEG', optimize=True, quality=85)
                    buf.seek(0)
                    b_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{filename}"
                    bc = blob_service_client.get_blob_client(container=AZURE_CONTAINER_NAME, blob=b_name)
                    bc.upload_blob(buf, overwrite=True)
                    file_url = bc.url
                    logger.info('Uploaded to Azure: %s', file_url)
                elif LOCAL_UPLOAD_FOLDER:
                    # Local fallback
                    logger.info('Saving photo locally for user %s', current_user.username)
                    b_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{filename}"
                    local_path = os.path.join(LOCAL_UPLOAD_FOLDER, b_name)
                    try:
                        img.save(local_path, format='JPEG', optimize=True, quality=85)
                    except Exception:
                        file.stream.seek(0)
                        with open(local_path, 'wb') as f:
                            f.write(file.stream.read())
                    file_url = url_for('static', filename=f'uploads/{b_name}', _external=True)
                else:
                    flash('No storage configured for uploads.', 'danger')
                    return render_template('dashboard.html')

                new_photo = Photo(filename=file_url, title=request.form.get('title'),
                                  caption=request.form.get('caption'), location=request.form.get('location'),
                                  people_present=request.form.get('people'), auto_tags=auto_tags, user_id=current_user.id)
                db.session.add(new_photo)
                db.session.commit()
                flash('✓ Photo uploaded!', 'success')
                return redirect(url_for('profile', username=current_user.username))
            except Exception:
                logger.exception('Photo upload failed')
                flash('Upload Error: see server logs', 'danger')
         # if no file, simply render the form
    return render_template('dashboard.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated: return redirect(url_for('feed'))
    if request.method == 'POST':
        role = request.form.get('role', 'consumer')
        if User.query.filter_by(username=request.form.get('username')).first():
            flash('Username taken', 'danger'); return redirect(url_for('register'))
        new_user = User(username=request.form.get('username'), role=role,
                        password=generate_password_hash(request.form.get('password')))
        db.session.add(new_user); db.session.commit()
        flash(f'Account created as {role.title()}! Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and check_password_hash(user.password, request.form.get('password')):
            if user.role == request.form.get('role'):
                login_user(user)
                return redirect(url_for('creator_dashboard' if user.role == 'creator' else 'feed'))
            flash(f'Incorrect Role! Registered as {user.role.title()}.', 'warning')
        else: flash('Invalid credentials', 'danger')
    return render_template('login.html')

@app.route('/edit_profile', methods=['GET', 'POST'])
@login_required
def edit_profile():
    if request.method == 'POST':
        current_user.bio = request.form.get('bio')
        avatar = request.files.get('avatar')
        if avatar and avatar.filename != '':
            try:
                avatar_filename = secure_filename(avatar.filename)
                if blob_service_client and AZURE_CONTAINER_NAME:
                    b_name = f"avatar_{current_user.id}_{avatar_filename}"
                    bc = blob_service_client.get_blob_client(container=AZURE_CONTAINER_NAME, blob=b_name)
                    bc.upload_blob(avatar, overwrite=True)
                    current_user.avatar = bc.url
                    logger.info('Uploaded avatar to Azure for user %s', current_user.username)
                else:
                    # Save locally
                    local_name = f"avatar_{current_user.id}_{avatar_filename}"
                    local_path = os.path.join(LOCAL_UPLOAD_FOLDER, local_name)
                    try:
                        img = Image.open(avatar)
                        if img.mode != 'RGB': img = img.convert('RGB')
                        img.thumbnail((400, 400))
                        img.save(local_path, format='JPEG', optimize=True, quality=85)
                    except Exception:
                        avatar.stream.seek(0)
                        with open(local_path, 'wb') as f:
                            f.write(avatar.stream.read())
                    current_user.avatar = local_name
                    logger.info('Saved avatar locally: %s', local_path)
            except Exception:
                logger.exception('Avatar upload failed for user %s', getattr(current_user, 'username', None))
        db.session.commit()
        flash('Profile updated!', 'success')
        return redirect(url_for('profile', username=current_user.username))
    return render_template('edit_profile.html')

@app.route('/comment/<int:photo_id>', methods=['POST'])
@login_required
def add_comment(photo_id):
    text = request.form.get('text')
    # Sentiment Analysis (Advanced Distinction Feature)
    score = TextBlob(text).sentiment.polarity
    if score < -0.3:
        return jsonify({'success': False, 'message': 'AI Blocked: Negative content! 🚫'})
    # classify sentiment for UI badge
    sentiment = 'neutral'
    if score >= 0.3: sentiment = 'positive'
    elif score <= -0.1: sentiment = 'negative'

    db.session.add(Comment(text=text, user_id=current_user.id, photo_id=photo_id))
    db.session.commit()
    return jsonify({'success': True, 'username': current_user.username, 'text': text, 'sentiment': sentiment})

@app.route('/like/<int:photo_id>', methods=['POST'])
@login_required
def toggle_like(photo_id):
    photo = Photo.query.get_or_404(photo_id)
    existing = Like.query.filter_by(user_id=current_user.id, photo_id=photo_id).first()
    if existing:
        db.session.delete(existing)
        liked = False
    else:
        db.session.add(Like(user_id=current_user.id, photo_id=photo_id))
        liked = True
    db.session.commit()
    count = photo.likes.count()
    return jsonify({'count': count, 'liked': liked})


@app.route('/save/<int:photo_id>', methods=['POST'])
@login_required
def toggle_save(photo_id):
    photo = Photo.query.get_or_404(photo_id)
    existing = Save.query.filter_by(user_id=current_user.id, photo_id=photo_id).first()
    if existing:
        db.session.delete(existing)
        saved = False
    else:
        db.session.add(Save(user_id=current_user.id, photo_id=photo_id))
        saved = True
    db.session.commit()
    return jsonify({'saved': saved})


@app.route('/post/<int:photo_id>/delete', methods=['POST'])
@login_required
def delete_post(photo_id):
    photo = Photo.query.get_or_404(photo_id)
    # Only the creator who owns the post may delete it
    if photo.user_id != current_user.id:
        return jsonify({'success': False, 'message': 'Not authorized'}), 403

    # Attempt to delete stored file (Azure blob or local)
    try:
        if photo.filename and photo.filename.startswith('http') and AZURE_CONTAINER_NAME and blob_service_client:
            # extract blob name after container path
            parts = photo.filename.split(f"/{AZURE_CONTAINER_NAME}/")
            if len(parts) == 2:
                blob_name = parts[1]
                try:
                    blob_service_client.get_blob_client(container=AZURE_CONTAINER_NAME, blob=blob_name).delete_blob()
                    logger.info('Deleted Azure blob: %s', blob_name)
                except Exception:
                    logger.exception('Failed deleting Azure blob %s', blob_name)
        else:
            # local file path handling
            if '/static/uploads/' in (photo.filename or ''):
                fname = photo.filename.split('/static/uploads/')[-1]
                p = os.path.join(LOCAL_UPLOAD_FOLDER, fname)
                if os.path.exists(p):
                    try:
                        os.remove(p)
                        logger.info('Deleted local file: %s', p)
                    except Exception:
                        logger.exception('Failed to delete local file: %s', p)
    except Exception:
        logger.exception('Error while attempting to remove stored file for photo %s', photo_id)

    try:
        db.session.delete(photo)
        db.session.commit()
    except Exception:
        logger.exception('Failed to delete photo record %s', photo_id)
        return jsonify({'success': False, 'message': 'DB delete failed'}), 500

    return jsonify({'success': True})


# --- Debug helper: list recent photos (only for logged-in creators) ---
@app.route('/debug/recent_photos', methods=['GET', 'POST'])
@login_required
def debug_recent_photos():
    if current_user.role != 'creator':
        flash('Access denied', 'danger')
        return redirect(url_for('feed'))

    if request.method == 'POST':
        # form-based delete for testing
        pid = request.form.get('photo_id')
        if pid:
            return delete_post(int(pid))

    photos = Photo.query.order_by(Photo.uploaded_at.desc()).limit(50).all()
    # simple HTML table for quick debugging
    rows = ['<h3>Recent Photos (creator debug)</h3>', '<table class="table"><tr><th>ID</th><th>Filename</th><th>Action</th></tr>']
    for p in photos:
        rows.append(f"<tr><td>{p.id}</td><td style='max-width:400px;word-break:break-all'>{p.filename}</td>"
                    f"<td><form method='post' style='display:inline'><input type='hidden' name='photo_id' value='{p.id}'/>"
                    f"<button class='btn btn-sm btn-danger' type='submit'>Delete</button></form></td></tr>")
    rows.append('</table>')
    return '\n'.join(rows)

@app.route('/logout')
@login_required
def logout():
    logout_user(); return redirect(url_for('login'))

@app.route('/_health')
def health():
    return jsonify({'status': 'ok', 'storage': bool(blob_service_client)})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)