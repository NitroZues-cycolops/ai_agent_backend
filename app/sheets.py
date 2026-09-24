import gspread
from google.oauth2.service_account import Credentials
from app.config import GOOGLE_CREDS_PATH, GOOGLE_SHEET_ID

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

def get_sheet_rows():
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_PATH, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1  # first tab
    rows = sheet.get_all_records(numericise_ignore=["all"])  # returns list of dicts, using row 1 as headers
    return rows