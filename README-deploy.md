This folder contains helper files and instructions for deploying the Streamlit app.

Recommended deploy targets:

1) Streamlit Community Cloud (quickest for public GitHub repos)
- Push the repo to GitHub.
- On https://share.streamlit.io, click "New app", pick this repository, branch (e.g., main), and `app.py` as the entrypoint.
- Add any secrets (DB URL, API keys) via the Streamlit app settings (`st.secrets`).

2) Other hosts (Render/Fly/Heroku)
- Use the included `Dockerfile` or `Procfile` at repo root.
- Ensure `requirements.txt` lists all dependencies.
- Configure environment variables/secrets in the host dashboard.

Pre-deploy checklist
- Ensure `requirements.txt` is up to date.
- Move local SQLite DB to a managed DB for production (recommended): Postgres, MySQL, or cloud file storage.
- Put credentials into host secrets or `secrets.toml` (Streamlit).

Files included here
- `.streamlit/config.toml` - recommended Streamlit server settings (port, headless mode).
- `.gitignore` - common ignores for Python and local DB files.

If you want, I can push these files to a new GitHub repo for you and trigger a deployment to Streamlit Cloud.