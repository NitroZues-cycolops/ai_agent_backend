in app/configs
-> import os, dotenv import load_dotenv
load_dotenv()
name = os.getenv("var_name")

valuerror so no crash if varname is not there
--> .env holds secrets locally, python-dotenv reads them into os.environ, and this file is the single place that pulls them out for the rest of your app to use. The raise ValueError is a safety net — if you forget to create .env or misspell the variable name, you get a clear crash immediately instead of a confusing error three files later.


-->.env — the actual secret values
DATABASE_URL=postgresql://prab:password@localhost:5432/hackathon_db

This is a connection string — one line that encodes everything needed to reach your database:

postgresql://  →  which database type/driver
prab           →  username
password       →  password
localhost      →  where the DB server is running (your own machine)
5432           →  Postgres's default port
hackathon_db   →  which database to connect to

@ is a reserved character in this URL format (it's the separator before the host) — keeping it in your actual password would break the parsing.
*** 

in app/database
create_engine(...) doesn't connect immediately — it creates a reusable connection manager that knows how to reach your DB whenever needed. Think of it as a phone number saved in contacts, not an active call.
echo=False — if True, it prints every SQL query it runs to your terminal (useful for debugging, noisy otherwise).

in app/model
Using SQLmodel is best choice as its combinatiuon of both the Pydantic and database defination. thus "table=True"

SQLModel.metadata as an invisible registry that keeps a list of every table class you've defined

SQLModel.metadata.create_all(engine) then does one job: look at everything in that registry, and for each one, check if it exists in the actual Postgres database — if not, run CREATE TABLE for it. engine is how it connects (the credentials/address you set up earlier

Session is a temporary workspace connected to your database — you open one, do some work (insert a row, query rows), then close it. Opening/closing has real cost (network handshake with Postgres), so you don't want one open permanently
***

app/main
--> uvicorn app.main:app --reload  

"yield" -->turns a function into a generator — instead of running start-to-finish and returning once, it can pause at yield, hand control back to whoever called it, and resume later from exactly where it paused.
async def marks a function as asynchronous — it can pause itself at await points, letting other code run during the wait, then resume once the awaited thing finishes. This matters hugely for a web server: while one request is waiting on a slow DB query, the server can serve other requests in the meantime, instead of freezing

Contextmanager --> Guarantees setup + cleanup code both run, even on error	Prevents resource leaks (unclosed files, DB connections)

@asynccontextmanager	Async version of @contextmanager	Same shortcut, for setup/cleanup that itself needs await

