from sqlmodel import create_engine, Session, SQLModel
from app.config import DATABASE_URL
from app.models import Run, Team, AuditLog  # Imported so all models are registered with SQLModel.metadata


engine = create_engine(DATABASE_URL, echo=False)

def create_db_and_tables():
    print("Creating tables...")
    SQLModel.metadata.create_all(engine)
    print("Tables created successfully.")

def get_session():
    with Session(engine) as session:
        yield session
