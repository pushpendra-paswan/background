from fastapi import FastAPI

from app.routes import imports

app = FastAPI(title="CSV Customer Import")

# Register the /imports endpoints
app.include_router(imports.router)
