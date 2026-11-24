from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import reserve


CONFIG = {
    "facility_id": "TEST", "facility_name": "안양천 테니스장(3코트)",
    "dates": ["2026-12-06"], "time": "10:00~12:00",
    "opens_at": "2026-11-25T10:00:00+09:00",
}


class ReservationTests(unittest.TestCase):
    def test_confirmation_requires_exact_court_date_time_and_completed_status(self):
        html = '''<table><tbody><tr><td class="reserv-date">2026-12-06</td>
        <td class="reserv-round">2회차 10:00 ~ 12:00</td>
        <td class="reserv-state">예약완료</td>
        <td class="reserv-subj">안양천 테니스장(3코트)</td></tr></tbody></table>'''
        self.assertTrue(reserve.matching_reservation(html, CONFIG, "2026-12-06"))
        for altered in (html.replace("3코트", "1코트"), html.replace("12-06", "12-13"),
                        html.replace("10:00", "08:00"), html.replace("예약완료", "예약취소")):
            self.assertFalse(reserve.matching_reservation(altered, CONFIG, "2026-12-06"))

    def test_closed_calendar_slot_is_not_open(self):
        html = '''<table><td><span class="date">6</span><li
        onclick="doReservationCalUserFormView('6', 'N', 'CLOSED', event)">
        <span class="time">10:00~12:00</span><p>예약대기</p></li></td></table>'''
        self.assertEqual(reserve.slot_state(html, "2026-12-06", "10:00~12:00"),
                         ("CLOSED", "N", "예약대기"))

    def test_form_does_not_opt_into_unchecked_optional_consent(self):
        _, data = reserve.form_data('''<form id="ReservationUserInfoVo">
        <input name="reservationToken" value="fresh">
        <input type="checkbox" name="agreeSms" value="Y">
        <input disabled name="disabled" value="x">
        <select name="slot"><option value="1">one</option>
        <option value="2" selected>two</option></select></form>''')
        self.assertEqual(data, {"reservationToken": "fresh", "slot": "2"})

    def test_execute_before_opening_cannot_submit(self):
        config = dict(CONFIG, opens_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        client = SimpleNamespace(authenticated=lambda: "")
        with TemporaryDirectory() as tmp, patch.object(reserve, "ROOT", Path(tmp)):
            with self.assertRaisesRegex(RuntimeError, "not open"):
                reserve.run(client, config, SimpleNamespace(execute=True, wait=False, dong="목1동"))

    def test_previous_uncertain_submission_is_never_retried(self):
        config = dict(CONFIG, opens_at="2020-01-01T10:00:00+09:00")
        client = SimpleNamespace(authenticated=lambda: "")
        with TemporaryDirectory() as tmp, patch.object(reserve, "ROOT", Path(tmp)):
            key = "TEST 2026-12-06 10:00~12:00"
            reserve.private_write(Path(tmp) / ".state/results.json", json.dumps({key: "unknown"}))
            # No calendar/request methods: any attempt to submit would fail this test.
            reserve.run(client, config, SimpleNamespace(execute=True, wait=False, dong="목1동"))


if __name__ == "__main__":
    unittest.main()
