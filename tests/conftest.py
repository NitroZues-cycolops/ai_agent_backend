"""Pytest configuration for test suite.

SAFETY: This conftest ensures TEST_DATABASE_URL is loaded and validated
BEFORE any tests run, and DATABASE_URL is redirected to TEST_DATABASE_URL
so the entire app uses the test database.

Tests REFUSE to run if:
1. TEST_DATABASE_URL is not set
2. The database name does not end in "_test"

This prevents tests from accidentally touching the dev database (hackathon_db).
"""
import os
import sys
import pytest
from dotenv import load_dotenv

# CRITICAL: Load .env and redirect DATABASE_URL BEFORE any app imports
load_dotenv()

# Check TEST_DATABASE_URL is set
test_db_url = os.getenv("TEST_DATABASE_URL")
if not test_db_url:
    pytest.exit(
        "FATAL: TEST_DATABASE_URL is not set in .env file.\n"
        "Tests REFUSE to run without a test database configured.",
        returncode=1
    )

# Extract database name and validate it ends with "_test"
db_name = test_db_url.split("/")[-1].split("?")[0]
if not db_name.endswith("_test"):
    pytest.exit(
        f"FATAL: Test database name must end in '_test'.\n"
        f"Got database: {db_name}\n"
        f"This safety check prevents tests from touching the dev database.",
        returncode=1
    )

# REDIRECT: Point DATABASE_URL to the test database so app.database.engine uses it
os.environ["DATABASE_URL"] = test_db_url

print(f"\n✓ Test database validated: {db_name}")
print(f"✓ DATABASE_URL redirected to TEST_DATABASE_URL")


# Import app modules AFTER redirection
from sqlmodel import SQLModel, create_engine

test_engine = create_engine(test_db_url)


@pytest.fixture(scope="function", autouse=True)
def reset_database():
    """Drop and recreate all tables before each test for full isolation."""
    SQLModel.metadata.drop_all(test_engine)
    SQLModel.metadata.create_all(test_engine)
    yield
