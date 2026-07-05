import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    # Default 8000, da macOS Port 5000 für AirPlay reserviert.
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
