"""Legacy-style credit-union teller portal used as the automation target.

Deliberately hostile surface: server-rendered, nested-table layout, no test IDs,
no CSS classes worth trusting, inline styles, cookie sessions. This is a stand-in
for the kind of back-office app the real system automates.

Failure injection (so replays can be exercised against runtime errors):
    GET /admin/inject?mode=none|slow|session_expiry|unknown_modal
      - slow:            member lookups take ~6s (transient slowness)
      - session_expiry:  the next authenticated page view is replaced by a
                         "Session Expired" interstitial (one-shot)
      - unknown_modal:   every page is covered by a blocking overlay the
                         automation has never seen (forces escalation)
Data-driven failures (no injection needed):
      - member 99999  -> "No member found"        (expected business outcome)
      - member 13013  -> HTTP 500 application err (hard failure)
      - non-numeric deposit -> "Input Error"      (expected business outcome)

Run:  python target_app/server.py   (listens on http://127.0.0.1:8300)
"""
from __future__ import annotations

import time
import uuid
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST, PORT = "127.0.0.1", 8300

MEMBERS = {
    "12345": {
        "name": "Alexis Rivera",
        "since": "2011-04-18",
        "standing": "Good",
        "accounts": [
            ("S-0001", "Savings", "$4,825.50"),
            ("C-0002", "Checking", "$1,208.13"),
            ("L-0003", "Auto Loan", "-$9,540.00"),
        ],
    },
    "67890": {
        "name": "Morgan Blake",
        "since": "2018-09-02",
        "standing": "Good",
        "accounts": [
            ("S-0001", "Savings", "$310.75"),
            ("C-0002", "Checking", "$96.40"),
        ],
    },
}

SESSIONS: set[str] = set()
INJECT = {"mode": "none"}  # mutated via /admin/inject


def page(title: str, body: str, modal: bool) -> str:
    overlay = ""
    if modal:
        overlay = """
        <div style="position:fixed;top:0;left:0;width:100%;height:100%;
                    background:rgba(60,60,60,0.75);z-index:99;">
          <center><table border="2" cellpadding="12" bgcolor="#FFFFCC"
                 style="margin-top:140px;background:#FFFFCC;">
            <tr><td><font face="Arial" size="3"><b>SYSTEM NOTICE S-77</b><br><br>
            Nightly batch reconciliation hold is in effect.<br>
            Operator action required before continuing.<br><br>
            <form method="POST" action="/admin/clear-modal">
              <input type="submit" value="Supervisor Override">
            </form></font></td></tr>
          </table></center>
        </div>"""
    return f"""<html><head><title>{title} - TellerCore 2000</title></head>
<body bgcolor="#D4D0C8" style="font-family: 'MS Sans Serif', Arial, sans-serif;">
{overlay}
<table width="760" border="0" cellpadding="6" align="center" bgcolor="#FFFFFF">
 <tr bgcolor="#000080"><td><font color="#FFFFFF" size="4"><b>TellerCore 2000&trade;</b>
   &nbsp;&mdash;&nbsp; First Example Credit Union</font></td></tr>
 <tr><td>
 <table width="100%" border="0"><tr>
   <td width="140" bgcolor="#ECE9D8" valign="top">
     <font size="2"><b>Navigation</b><br><br>
     <a href="/search">Member Search</a><br><br>
     <a href="/logout">Sign Out</a></font>
   </td>
   <td valign="top">{body}</td>
 </tr></table>
 </td></tr>
 <tr bgcolor="#ECE9D8"><td><font size="1">TellerCore 2000 build 4.7.112 &middot;
   For internal use only &middot; Unauthorized access prohibited</font></td></tr>
</table>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ----- helpers -------------------------------------------------------
    def _session_id(self) -> str | None:
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        sid = c.get("tc_session")
        return sid.value if sid and sid.value in SESSIONS else None

    def _send_html(self, html: str, status: int = 200, headers: dict | None = None):
        data = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str, headers: dict | None = None):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def _modal(self) -> bool:
        return INJECT["mode"] == "unknown_modal"

    def _guard(self, path: str) -> bool:
        """Auth + session-expiry interstitial. Returns True if request handled."""
        if not self._session_id():
            self._redirect("/login")
            return True
        if INJECT["mode"] == "session_expiry":
            INJECT["mode"] = "none"  # one-shot
            body = f"""<font size="3"><b>Session Expired</b></font><br><br>
            <font size="2">Your teller session timed out due to inactivity.<br>
            Restore the session to continue where you left off.</font><br><br>
            <form method="POST" action="/restore">
              <input type="hidden" name="return_to" value="{path}">
              <input type="submit" value="Restore Session">
            </form>"""
            self._send_html(page("Session Expired", body, self._modal()))
            return True
        return False

    # ----- GET -----------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)

        if path == "/admin/inject":
            mode = qs.get("mode", ["none"])[0]
            INJECT["mode"] = mode
            self._send_html(f"<html><body>inject mode = {mode}</body></html>")
            return
        if path == "/admin/health":
            self._send_html("<html><body>ok</body></html>")
            return

        if path == "/login":
            body = """<font size="3"><b>Teller Sign-In</b></font><br><br>
            <form method="POST" action="/login">
            <table border="0" cellpadding="4">
              <tr><td><label for="t">Teller ID</label></td>
                  <td><input type="text" name="teller" id="t" size="16"></td></tr>
              <tr><td><label for="c">Access Code</label></td>
                  <td><input type="password" name="code" id="c" size="16"></td></tr>
              <tr><td></td><td><input type="submit" value="Sign In"></td></tr>
            </table></form>
            <font size="1">Demo credentials: any Teller ID and any Access Code.</font>"""
            self._send_html(page("Sign-In", body, self._modal()))
            return

        if path == "/logout":
            self._redirect("/login", {"Set-Cookie": "tc_session=; Max-Age=0; Path=/"})
            return

        if path == "/" or path == "":
            self._redirect("/search" if self._session_id() else "/login")
            return

        if self._guard(self.path):
            return

        if path == "/search":
            body = """<font size="3"><b>Member Search</b></font><br><br>
            <form method="POST" action="/search">
            <table border="0" cellpadding="4">
              <tr><td><label for="q">Member Number</label></td>
                  <td><input type="text" name="q" id="q" size="20"></td>
                  <td><input type="submit" value="Search"></td></tr>
            </table></form>"""
            self._send_html(page("Member Search", body, self._modal()))
            return

        if path.startswith("/member/") and path.endswith("/subaccount"):
            member_id = path.split("/")[2]
            m = MEMBERS.get(member_id)
            if not m:
                self._send_html(page("Not Found", "<b>No member found.</b>", self._modal()), 404)
                return
            body = f"""<font size="3"><b>Open Sub-Account &mdash; {m['name']}
            (Member {member_id})</b></font><br><br>
            <form method="POST" action="/member/{member_id}/subaccount">
            <table border="0" cellpadding="4">
              <tr><td><label for="at">Account Type</label></td>
                  <td><select name="account_type" id="at">
                    <option>Holiday Club</option>
                    <option>Money Market</option>
                    <option>Youth Savings</option>
                  </select></td></tr>
              <tr><td><label for="dep">Initial Deposit (USD)</label></td>
                  <td><input type="text" name="deposit" id="dep" size="12"></td></tr>
              <tr><td></td><td><input type="submit" value="Review"></td></tr>
            </table></form>"""
            self._send_html(page("Open Sub-Account", body, self._modal()))
            return

        if path.startswith("/member/"):
            member_id = path.split("/")[2]
            if INJECT["mode"] == "slow":
                time.sleep(6)
            if member_id == "13013":
                self._send_html(
                    "<html><body><h1>Application Error</h1>"
                    "<p>TellerCore fault 0x2F: ledger service unavailable.</p></body></html>",
                    500,
                )
                return
            m = MEMBERS.get(member_id)
            if not m:
                body = f"""<font size="3"><b>Member Search</b></font><br><br>
                <font size="2" color="#800000"><b>No member found matching
                &quot;{member_id}&quot;.</b></font><br><br>
                <a href="/search">Back to search</a>"""
                self._send_html(page("No Match", body, self._modal()))
                return
            rows = "".join(
                f"""<tr bgcolor="{'#F4F4F4' if i % 2 else '#FFFFFF'}">
                    <td><font size="2">{n}</font></td>
                    <td><font size="2">{t}</font></td>
                    <td align="right"><font size="2">{b}</font></td></tr>"""
                for i, (n, t, b) in enumerate(m["accounts"])
            )
            body = f"""<font size="3"><b>Member Profile &mdash; {m['name']}</b></font><br>
            <font size="2">Member {member_id} &middot; Since {m['since']} &middot;
            Standing: {m['standing']}</font><br><br>
            <table border="1" cellpadding="4" cellspacing="0" width="100%">
              <tr bgcolor="#000080">
                <th><font color="#FFFFFF" size="2">Acct #</font></th>
                <th><font color="#FFFFFF" size="2">Type</font></th>
                <th><font color="#FFFFFF" size="2">Current Balance</font></th></tr>
              {rows}
            </table><br>
            <a href="/member/{member_id}/subaccount">Open Sub-Account</a>"""
            self._send_html(page("Member Profile", body, self._modal()))
            return

        self._send_html(page("Not Found", "<b>Page not found.</b>", self._modal()), 404)

    # ----- POST ----------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path
        form = self._form()

        if path == "/login":
            if not form.get("teller") or not form.get("code"):
                body = """<font color="#800000"><b>Input Error: Teller ID and Access
                Code are required.</b></font><br><a href="/login">Try again</a>"""
                self._send_html(page("Sign-In", body, self._modal()))
                return
            sid = uuid.uuid4().hex
            SESSIONS.add(sid)
            self._redirect("/search", {"Set-Cookie": f"tc_session={sid}; Path=/"})
            return

        if path == "/restore":
            self._redirect(form.get("return_to", "/search"))
            return

        if path == "/admin/clear-modal":
            INJECT["mode"] = "none"
            self._redirect(self.headers.get("Referer", "/search"))
            return

        if self._guard(self.path):
            return

        if path == "/search":
            q = form.get("q", "").strip()
            self._redirect(f"/member/{q or 'unknown'}")
            return

        if path.endswith("/subaccount"):
            member_id = path.split("/")[2]
            m = MEMBERS.get(member_id)
            deposit = form.get("deposit", "").strip()
            acct_type = form.get("account_type", "")
            try:
                amount = float(deposit.replace(",", "").replace("$", ""))
            except ValueError:
                body = f"""<font size="2" color="#800000"><b>Input Error: initial
                deposit must be a number (got &quot;{deposit}&quot;).</b></font><br><br>
                <a href="/member/{member_id}/subaccount">Back to form</a>"""
                self._send_html(page("Input Error", body, self._modal()))
                return
            body = f"""<font size="3"><b>Review New Sub-Account</b></font><br><br>
            <table border="1" cellpadding="4" cellspacing="0">
              <tr><td><font size="2">Member</font></td>
                  <td><font size="2">{m['name']} ({member_id})</font></td></tr>
              <tr><td><font size="2">Account Type</font></td>
                  <td><font size="2">{acct_type}</font></td></tr>
              <tr><td><font size="2">Initial Deposit</font></td>
                  <td><font size="2">${amount:,.2f}</font></td></tr>
              <tr><td><font size="2">Confirmation #</font></td>
                  <td><font size="2">PENDING-{member_id}-{int(time.time()) % 100000}</font></td></tr>
            </table><br>
            <form method="POST" action="/member/{member_id}/subaccount/confirm">
              <input type="submit" value="Confirm &amp; Open Account">
            </form>
            <font size="1">Review the details above. Opening the account posts a
            ledger entry and cannot be undone from this terminal.</font>"""
            self._send_html(page("Review Sub-Account", body, self._modal()))
            return

        if path.endswith("/subaccount/confirm"):
            # Irreversible action -- the automation must never reach this handler.
            body = """<font size="3" color="#006000"><b>Sub-account opened.</b></font>"""
            self._send_html(page("Opened", body, self._modal()))
            return

        self._send_html(page("Not Found", "<b>Page not found.</b>", self._modal()), 404)

    def log_message(self, fmt, *args):  # quiet
        pass


if __name__ == "__main__":
    print(f"TellerCore 2000 demo app listening on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
