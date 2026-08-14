#!/usr/bin/env python3
"""Interactive Google Drive OAuth Setup CLI for CodingAssistant.

Run this script once in a terminal to authenticate Google Drive:
    python daemon/setup_gdrive_oauth.py

It launches a local HTTP server, prompts the user to open Google's consent screen,
catches the OAuth callback, exchanges the code for a refresh token, and saves
credentials to %LOCALAPPDATA%/CodingAssistant/gdrive_credentials.json.
"""

import http.server
import json
import os
import socketserver
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

SCOPES = "https://www.googleapis.com/auth/drive.metadata.readonly"
REDIRECT_PORT = 8085
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

def get_save_path() -> Path:
    local_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    dir_path = local_appdata / "CodingAssistant"
    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path / "gdrive_credentials.json"

class OAuthCallbackHandler(http.server.SimpleHTTPRequestHandler):
    auth_code = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/callback":
            query = urllib.parse.parse_qs(parsed.query)
            if "code" in query:
                OAuthCallbackHandler.auth_code = query["code"][0]
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Google Drive Authenticated Successfully!</h1><p>You can close this tab and return to your terminal.</p>")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"<h1>Authentication Failed</h1>")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

def main():
    print("==================================================")
    print("      CodingAssistant — Google Drive OAuth 2.0 Setup      ")
    print("==================================================")
    print("\nTo connect Google Drive, you need a Google Cloud OAuth Client ID & Secret.")
    print("If you already have them, enter them below:\n")
    
    client_id = input("Google OAuth Client ID: ").strip()
    if not client_id:
        print("Error: Client ID is required.")
        return

    client_secret = input("Google OAuth Client Secret: ").strip()
    if not client_secret:
        print("Error: Client Secret is required.")
        return

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"

    print(f"\nOpening browser for authorization...")
    print(f"If the browser doesn't open automatically, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    print(f"Waiting for authorization on localhost:{REDIRECT_PORT}...")
    with socketserver.TCPServer(("localhost", REDIRECT_PORT), OAuthCallbackHandler) as httpd:
        while OAuthCallbackHandler.auth_code is None:
            httpd.handle_request()

    code = OAuthCallbackHandler.auth_code
    print("Authorization code received! Exchanging for tokens...")

    token_url = "https://oauth2.googleapis.com/token"
    token_payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }

    resp = httpx.post(token_url, data=token_payload)
    if resp.status_code != 200:
        print(f"Error exchanging code for token: {resp.text}")
        return

    tokens = resp.json()
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("Warning: Google did not return a refresh token (did you already authorize this app? prompt=consent should force it).")

    creds_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }

    save_path = get_save_path()
    save_path.write_text(json.dumps(creds_data, indent=2), encoding="utf-8")
    print(f"\n Credentials successfully saved to:\n  {save_path}")
    print("\nGoogle Drive is now ready to be monitored by CodingAssistant!")

if __name__ == "__main__":
    main()
