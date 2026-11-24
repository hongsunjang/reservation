"""Yangcheon booking helper. Preview is the default; mutations require --execute."""
import argparse
from datetime import datetime
import getpass
import http.cookiejar
import json
import os
from pathlib import Path
import re
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://www.yangcheon.go.kr"
CALENDAR = "/reservation/reservation/ex/reservationCal/View.do"
FORM = "/reservation/reservation/foffice/ex/reservationCal/userForm.do"
CONFIRM = "/reservation/reservation/foffice/ex/reservationCal/facConfirmAx.do"
REGISTER = "/reservation/reservation/foffice/ex/reservationCal/userReg.do"
MY = "/reservation/reservation/foffice/ex/reservationMy/list.do"
ROOT = Path(__file__).resolve().parent


def log(message):
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}", flush=True)


def soup(html):
    return BeautifulSoup(html, "html.parser")


def private_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(content)


def compact(text):
    return re.sub(r"\s+", "", text)


def form_data(html):
    form = soup(html).select_one("form#ReservationUserInfoVo")
    if form is None:
        raise RuntimeError("Reservation form missing; authentication or site flow changed")
    data = {}
    for field in form.select("input[name], select[name], textarea[name]"):
        if field.has_attr("disabled"):
            continue
        if field.get("type") in ("checkbox", "radio") and not field.has_attr("checked"):
            continue
        if field.name == "select":
            option = field.select_one("option[selected]") or field.select_one("option")
            data[field["name"]] = option.get("value", option.get_text()) if option else ""
        elif field.name == "textarea":
            data[field["name"]] = field.get_text()
        else:
            data[field["name"]] = field.get("value", "")
    return form, data


def matching_reservation(html, config, date):
    for row in soup(html).select("table tbody tr"):
        cells = {k: row.select_one(f".reserv-{k}") for k in ("date", "round", "state", "subj")}
        if not all(cells.values()):
            continue
        if (date == cells["date"].get_text(strip=True)
                and compact(config["time"]) in compact(cells["round"].get_text())
                and compact(config["facility_name"]) == compact(cells["subj"].get_text())):
            status = cells["state"].get_text(strip=True)
            if status == "예약완료":
                return True
            if "취소" not in status:
                raise RuntimeError(f"Matching reservation has unresolved status: {status}")
    return False


def slot_state(html, date, target_time):
    day = str(int(date[-2:]))
    for cell in soup(html).select("td"):
        label = cell.select_one(".date")
        if label is None or label.get_text(strip=True) != day:
            continue
        for item in cell.select("li"):
            clock = item.select_one(".time")
            if clock and compact(clock.get_text()) == compact(target_time):
                call = item.get("onclick", "")
                match = re.search(r"doReservationCalUserFormView\('([^']+)',\s*'([^']*)',\s*'([^']+)'", call)
                state = item.select_one("p")
                return (match.group(3) if match else "UNKNOWN", match.group(2) if match else "", state.get_text(strip=True) if state else "")
    raise RuntimeError(f"Calendar slot missing for {date}")


class Client:
    def __init__(self, cookie_path):
        self.cookie_path = Path(cookie_path)
        self.session = requests.Session()
        self.session.cookies = http.cookiejar.LWPCookieJar(str(self.cookie_path))
        if self.cookie_path.exists():
            self.session.cookies.load(ignore_discard=True, ignore_expires=True)

    def request(self, method, path, **kwargs):
        r = self.session.request(method, BASE + path, timeout=20, **kwargs)
        r.raise_for_status()
        return r

    def save(self):
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.cookie_path.exists():
            private_write(self.cookie_path, "")
        os.chmod(self.cookie_path, 0o600)
        self.session.cookies.save(ignore_discard=True, ignore_expires=True)

    def authenticated(self):
        html = self.request("GET", MY).text
        if "로그아웃" not in html:
            raise RuntimeError("Login session expired; run login again")
        self.save()
        return html

    def login(self, user):
        path = "/site/yangcheon/login/Login.do"
        self.request("GET", path)
        password = getpass.getpass("Password (not saved): ")
        self.request("POST", "/site/yangcheon/login/Login_List.do", data={
            "cuId": user, "cuPwd": password, "returnurl": MY,
        }, headers={"Referer": BASE + path})
        del password
        self.authenticated()
        log("Login successful")

    def calendar(self, config, date):
        year, month, _ = date.split("-")
        return self.request("GET", CALENDAR, params={
            "riIdx": config["facility_id"], "year": year, "month": month, "tab": "R",
        }).text

    def prepare(self, config, date, dong, rest_at="N"):
        year, month, day = date.split("-")
        html = self.request("GET", FORM, params={
            "riIdx": config["facility_id"], "year": year, "month": month,
            "day": str(int(day)), "pageIndex": "1", "tab": "R", "timeweek": "", "restAt": rest_at,
        }).text
        form, data = form_data(html)
        if data.get("riIdx") != config["facility_id"] or data.get("ruReservationDay") != date:
            raise RuntimeError("Form facility/date mismatch")
        options = form.select("#stfacTime option")
        option = next((o for o in options if compact(config["time"]) in compact(o.get_text()) and not o.has_attr("disabled")), None)
        if option is None:
            raise RuntimeError("Requested time not available in form")
        if dong not in [o.get("value") for o in form.select("#dong option")]:
            raise RuntimeError("Invalid neighborhood")
        if "양천구" not in data.get("ruAddress", ""):
            raise RuntimeError("Account address does not meet residency requirement")
        for key in ("ruName", "ruZipcode", "ruAddress", "ruHp1", "ruHp2", "ruHp3", "reservationToken"):
            if not data.get(key):
                raise RuntimeError(f"Required account field missing: {key}")
        if form.select('input[name*="captcha" i]'):
            raise RuntimeError("Human verification required")
        data.update(stfacTime=option["value"], dong=dong, personck="on")
        data["ruHp"] = "-".join(data[f"ruHp{i}"] for i in (1, 2, 3))
        data["ruTel"] = "-".join(data.get(f"ruTel{i}", "") for i in (1, 2, 3))
        data.pop("agreeSms", None)  # Optional promotional messages remain unchecked.
        return data, int(option["cnt"])


def run(client, config, args):
    opens_at = datetime.fromisoformat(config["opens_at"])
    if opens_at.tzinfo is None:
        raise RuntimeError("opens_at must include timezone")
    client.authenticated()
    if not args.execute:
        for date in config["dates"]:
            state, rest_at, label = slot_state(client.calendar(config, date), date, config["time"])
            client.prepare(config, date, args.dong, rest_at)
            log(f"Preview {date} {config['time']}: {label}; required fields ready; no submission")
        return

    ledger_path = ROOT / ".state" / "results.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    now = lambda: datetime.now(opens_at.tzinfo)
    if now() < opens_at and not args.wait:
        raise RuntimeError("Registration is not open; use --wait to wait")
    next_keepalive = 0.0
    while now() < opens_at:
        if time.monotonic() >= next_keepalive:
            client.authenticated()
            log(f"Waiting until {opens_at.isoformat()}; session active")
            next_keepalive = time.monotonic() + 240
        time.sleep(min(30, max(0.05, (opens_at - now()).total_seconds())))

    for date in config["dates"]:
        key = f"{config['facility_id']} {date} {config['time']}"
        if matching_reservation(client.authenticated(), config, date):
            ledger[key] = "confirmed"
            private_write(ledger_path, json.dumps(ledger, ensure_ascii=False, indent=2))
            log(f"Confirmed existing reservation: {date}")
            continue
        if ledger.get(key) in ("attempted", "unknown", "confirmed"):
            log(f"Manual reconciliation required for {date}; refusing duplicate submission")
            continue
        state, rest_at, label = slot_state(client.calendar(config, date), date, config["time"])
        # A brief delay in the site's opening is allowed; no bypass of CLOSED slots.
        deadline = time.monotonic() + 60
        while state != "OPEN" and label == "예약대기" and time.monotonic() < deadline:
            time.sleep(2)
            state, rest_at, label = slot_state(client.calendar(config, date), date, config["time"])
        if state != "OPEN":
            log(f"Unavailable {date}: {label}; no submission")
            ledger[key] = "unavailable"
            private_write(ledger_path, json.dumps(ledger, ensure_ascii=False, indent=2))
            continue
        data, capacity = client.prepare(config, date, args.dong, rest_at)
        availability = client.request("GET", CONFIRM, params={
            "riIdx": data["riIdx"], "raIdx": data["raIdx"],
            "ruReservationDay": date, "rtmIdx": data["stfacTime"],
        }).json()
        if capacity - int(availability.get("confirmCnt") or 0) < int(data["ruObjectCnt"]):
            log(f"Unavailable {date}: capacity filled")
            continue
        if now() < opens_at:
            raise RuntimeError("Refusing submission before opening")
        ledger[key] = "attempted"
        private_write(ledger_path, json.dumps(ledger, ensure_ascii=False, indent=2))
        try:
            response = client.request("POST", REGISTER, data=data, headers={"Referer": BASE + FORM})
            private_write(ROOT / ".state" / f"response-{date}.html", response.text)
        except requests.RequestException:
            log(f"Submission response uncertain for {date}; checking account without retry")
        confirmed = False
        for attempt in range(3):
            if attempt:
                time.sleep(1)
            if matching_reservation(client.authenticated(), config, date):
                confirmed = True
                break
        ledger[key] = "confirmed" if confirmed else "unknown"
        private_write(ledger_path, json.dumps(ledger, ensure_ascii=False, indent=2))
        log(f"{date}: {ledger[key]}")
    log("Run finished; inspect .state/results.json for verified outcomes")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "booking.json"))
    parser.add_argument("--cookies", default=str(ROOT / ".state" / "session.cookies"))
    parser.add_argument("--login", metavar="USER")
    parser.add_argument("--dong")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    client = Client(args.cookies)
    if args.login:
        client.login(args.login)
        return
    if not args.dong:
        parser.error("--dong is required")
    # A nonblocking process lock prevents overlapping booking runners.
    import fcntl
    lock_path = ROOT / ".state" / "runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(client, json.loads(Path(args.config).read_text()), args)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Exception bodies/URLs can contain account fields; report only the type.
        log(f"Stopped ({type(error).__name__}); inspect locally before restarting")
        raise SystemExit(1)
