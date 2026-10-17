from fastapi import FastAPI

from app.logging_config import setup_logging
from app.routes import admin, imports

# Every log line of the API is one JSON object
setup_logging("api")

app = FastAPI(title="CSV Customer Import")

# Register the /imports endpoints and the admin endpoints (/stats, retry, resend-email)
app.include_router(imports.router)
app.include_router(admin.router)
