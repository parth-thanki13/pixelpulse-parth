from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()

# Followers Table (Association Table)
followers = db.Table('followers',
    db.Column('follower_id', db.Integer, db.ForeignKey('user.id')),
    db.Column('followed_id', db.Integer, db.ForeignKey('user.id'))
)

class Like(db.Model):
    __tablename__ = 'likes'
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    photo_id = db.Column(db.Integer, db.ForeignKey('photo.id'), primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Save(db.Model):
    __tablename__ = 'saves'
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), primary_key=True)
    photo_id = db.Column(db.Integer, db.ForeignKey('photo.id'), primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(500), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    photo_id = db.Column(db.Integer, db.ForeignKey('photo.id'), nullable=False)
    user = db.relationship('User', backref='comments')

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    # FIX: Password limit increased to 255 to handle scrypt hashing
    password = db.Column(db.String(255), nullable=False) 
    role = db.Column(db.String(50), nullable=False, default='consumer')
    bio = db.Column(db.String(300))
    avatar = db.Column(db.String(100))
    
    liked_photos = db.relationship('Like', backref='user', lazy='dynamic')
    saved_photos = db.relationship('Save', backref='user', lazy='dynamic')
    
    followed = db.relationship(
        'User', secondary=followers,
        primaryjoin=(followers.c.follower_id == id),
        secondaryjoin=(followers.c.followed_id == id),
        backref=db.backref('followers', lazy='dynamic'), lazy='dynamic')

    def follow(self, user):
        if not self.is_following(user): self.followed.append(user)
    def unfollow(self, user):
        if self.is_following(user): self.followed.remove(user)
    def is_following(self, user):
        return self.followed.filter(followers.c.followed_id == user.id).count() > 0

class Photo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # FIX: Filename limit 255 to store long S3 URLs
    filename = db.Column(db.String(255), nullable=False)
    title = db.Column(db.String(100), nullable=False)
    caption = db.Column(db.String(500))
    location = db.Column(db.String(100))
    people_present = db.Column(db.String(200))
    
    # --- AUTOMATED MEDIA ANALYSIS TAGS ---
    auto_tags = db.Column(db.String(300)) 
    
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    
    creator = db.relationship('User', backref='photos')
    likes = db.relationship('Like', backref='photo', lazy='dynamic')
    saves = db.relationship('Save', backref='photo', lazy='dynamic')
    comments = db.relationship('Comment', backref='photo', lazy='dynamic', cascade="all, delete-orphan")

    def is_liked_by(self, user):
        return self.likes.filter_by(user_id=user.id).count() > 0
    def is_saved_by(self, user):
        return self.saves.filter_by(user_id=user.id).count() > 0