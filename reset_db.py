from app import app, db

with app.app_context():
    print("Purane tables delete ho rahe hain...")
    db.drop_all()  # DELETE command

    print("Naye tables ban rahe hain...")
    db.create_all() # CREATE command
    print("Done! ✅")