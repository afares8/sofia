"""Start Sofia Monitor backend."""
import uvicorn
import os
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    host = os.getenv("SOFIA_HOST", "0.0.0.0")
    port = int(os.getenv("SOFIA_PORT", "9000"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
