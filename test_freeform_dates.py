import unittest
import datetime
import zoneinfo
from unittest.mock import AsyncMock, patch
from bot.utils.date_helper import (
    calculate_future_date,
    calculate_future_date_details,
)
from bot.ai.tools import execute_tool, apply_action_draft_reply
from bot.utils.action_drafts import (
    create_action_draft,
    get_action_draft,
    DRAFT_STATUS_AWAITING_INFO,
    DRAFT_STATUS_PENDING_CONFIRMATION,
)
from bot.database.session import AsyncSessionLocal
from bot.database.models import ActionDraft


class TestFreeformDateHelper(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tz_kyiv = "Europe/Kyiv"
        self.tz_utc = "UTC"

    def test_weekdays_ukrainian_apostrophes(self):
        """Verify that all Ukrainian apostrophe variants for Friday resolve correctly to future Friday."""
        variants = [
            "наступної п'ятниці",
            "наступної пʼятниці",
            "наступної п’ятниці",
            "наступної пятниці",
        ]
        for v in variants:
            iso, local_str, w_en, w_uk = calculate_future_date_details(v, self.tz_kyiv)
            self.assertIsNotNone(iso, f"Failed for variant: {v}")
            self.assertEqual(w_en, "Friday", f"Expected Friday for: {v}")
            self.assertEqual(w_uk, "П'ятниця", f"Expected П'ятниця for: {v}")
            self.assertIn("09:00:00", local_str)

    def test_weekdays_with_time(self):
        """Verify that specific times (e.g. о 15:00, в 15:30, о 15) are correctly parsed."""
        t1 = calculate_future_date_details("наступної п'ятниці о 15:00", self.tz_kyiv)
        self.assertIsNotNone(t1[0])
        self.assertIn("15:00:00", t1[1])
        self.assertEqual(t1[2], "Friday")

        t2 = calculate_future_date_details("наступної п'ятниці в 15:30", self.tz_kyiv)
        self.assertIsNotNone(t2[0])
        self.assertIn("15:30:00", t2[1])

        t3 = calculate_future_date_details("наступної п'ятниці о 18", self.tz_kyiv)
        self.assertIsNotNone(t3[0])
        self.assertIn("18:00:00", t3[1])

    def test_weekdays_different_days(self):
        """Verify that different weekdays in Ukrainian resolve correctly."""
        days = [
            ("наступного понеділка", "Monday", "Понеділок"),
            ("наступного вівторка", "Tuesday", "Вівторок"),
            ("наступної середи", "Wednesday", "Середа"),
            ("наступного четверга", "Thursday", "Четвер"),
            ("наступної суботи", "Saturday", "Субота"),
            ("наступної неділі", "Sunday", "Неділя"),
        ]
        for phrase, exp_en, exp_uk in days:
            iso, local_str, w_en, w_uk = calculate_future_date_details(phrase, self.tz_kyiv)
            self.assertIsNotNone(iso, f"Failed for {phrase}")
            self.assertEqual(w_en, exp_en, f"Expected {exp_en} for {phrase}")
            self.assertEqual(w_uk, exp_uk, f"Expected {exp_uk} for {phrase}")

    def test_relative_days(self):
        """Verify relative day keywords: завтра, післязавтра."""
        z = calculate_future_date_details("завтра", self.tz_kyiv)
        self.assertIsNotNone(z[0])
        self.assertIn("09:00:00", z[1])

        z_time = calculate_future_date_details("завтра о 18:00", self.tz_kyiv)
        self.assertIsNotNone(z_time[0])
        self.assertIn("18:00:00", z_time[1])

        pz = calculate_future_date_details("післязавтра вранці", self.tz_kyiv)
        self.assertIsNotNone(pz[0])
        self.assertIn("09:00:00", pz[1])

    def test_relative_intervals(self):
        """Verify relative intervals: через 2 години, через 15 хвилин, через півгодини, через тиждень."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        iso2h = calculate_future_date("через 2 години", self.tz_kyiv)
        dt2h = datetime.datetime.fromisoformat(iso2h)
        diff2h = (dt2h - now_utc).total_seconds()
        self.assertAlmostEqual(diff2h, 7200, delta=60)

        iso15m = calculate_future_date("через 15 хвилин", self.tz_kyiv)
        dt15m = datetime.datetime.fromisoformat(iso15m)
        diff15m = (dt15m - now_utc).total_seconds()
        self.assertAlmostEqual(diff15m, 900, delta=60)

        iso_half = calculate_future_date("через півгодини", self.tz_kyiv)
        dt_half = datetime.datetime.fromisoformat(iso_half)
        diff_half = (dt_half - now_utc).total_seconds()
        self.assertAlmostEqual(diff_half, 1800, delta=60)

        iso_w = calculate_future_date("через тиждень о 12:00", self.tz_kyiv)
        dt_w = datetime.datetime.fromisoformat(iso_w)
        diff_w = (dt_w - now_utc).total_seconds()
        self.assertAlmostEqual(diff_w, 7 * 86400, delta=86400)

    def test_parts_of_day_words(self):
        """Verify time of day words: вранці, вдень, ввечері, вночі."""
        res_m = calculate_future_date_details("завтра вранці", self.tz_kyiv)
        self.assertIn("09:00:00", res_m[1])

        res_d = calculate_future_date_details("завтра вдень", self.tz_kyiv)
        self.assertIn("13:00:00", res_d[1])

        res_e = calculate_future_date_details("завтра ввечері", self.tz_kyiv)
        self.assertIn("19:00:00", res_e[1])

        res_n = calculate_future_date_details("завтра вночі", self.tz_kyiv)
        self.assertIn("22:00:00", res_n[1])

    def test_english_expressions(self):
        """Verify English expressions: next friday at 15:00, tomorrow at 10am, in 2 hours."""
        res = calculate_future_date_details("next friday at 15:00", self.tz_kyiv)
        self.assertIsNotNone(res[0])
        self.assertEqual(res[2], "Friday")
        self.assertIn("15:00:00", res[1])

        res_tom = calculate_future_date_details("tomorrow at 10am", self.tz_kyiv)
        self.assertIsNotNone(res_tom[0])
        self.assertIn("10:00:00", res_tom[1])

    def test_backward_compatibility_strict_iso(self):
        """Verify strict YYYY-MM-DD HH:MM:SS format continues to work identically."""
        res = calculate_future_date("2028-10-15 14:30:00", "UTC")
        self.assertIn("2028-10-15T14:30:00", res)

    def test_invalid_and_empty_inputs(self):
        """Verify invalid or empty inputs return clear error messages."""
        res_empty = calculate_future_date("   ", self.tz_kyiv)
        self.assertTrue(res_empty.startswith("Error"))

        res_invalid = calculate_future_date("some completely unparseable text", self.tz_kyiv)
        self.assertTrue(res_invalid.startswith("Error"))

        details = calculate_future_date_details("garbage input", self.tz_kyiv)
        self.assertEqual(details, (None, None, None, None))


from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from bot.database.models import Base


class TestFreeformToolsIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.SessionLocal = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.patcher = patch("bot.utils.action_drafts.AsyncSessionLocal", self.SessionLocal)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.engine.dispose()

    async def test_execute_tool_calculate_date_rich_payload(self):
        """Verify calculate_date returns rich metadata payload."""
        res = await execute_tool(
            "calculate_date",
            {"local_datetime": "наступної пʼятниці о 15:00"},
            timezone_name="Europe/Kyiv"
        )
        self.assertTrue(res.payload.get("success"))
        self.assertIsNotNone(res.payload.get("iso_time_utc"))
        self.assertIn("15:00:00", res.payload.get("local_datetime", ""))
        self.assertEqual(res.payload.get("weekday"), "Friday")
        self.assertEqual(res.payload.get("weekday_uk"), "П'ятниця")
        self.assertEqual(res.payload.get("timezone"), "Europe/Kyiv")
        self.assertIsNotNone(res.payload.get("current_datetime"))
        self.assertFalse(res.stop)

    async def test_schedule_reminder_with_natural_language_draft_fallback(self):
        """Verify schedule_reminder directly parses natural language dates into draft ISO UTC time."""
        res = await execute_tool(
            "schedule_reminder",
            {"iso_time_utc": "наступної п'ятниці о 15:00", "text": "подзвонити лікарю"},
            user_id=101,
            chat_id=202,
            timezone_name="Europe/Kyiv",
        )
        self.assertTrue(res.payload.get("success"))
        self.assertTrue(res.stop)
        self.assertEqual(res.payload.get("status"), DRAFT_STATUS_PENDING_CONFIRMATION)
        self.assertEqual(res.payload.get("missing_fields"), [])
        self.assertIn("подзвонити лікарю", res.display_text)
        self.assertIn("15:00", res.display_text)

        # Check DB draft record
        draft = await get_action_draft(res.draft_id, 101, 202)
        self.assertIsNotNone(draft)
        self.assertEqual(draft.status, DRAFT_STATUS_PENDING_CONFIRMATION)
        self.assertIn("T", draft.payload.get("iso_time_utc", ""))
        self.assertEqual(draft.payload.get("text"), "подзвонити лікарю")

    async def test_apply_action_draft_reply_with_freeform_date(self):
        """Verify user clarification reply with 'наступної п'ятниці' resolves draft to pending_confirmation."""
        # Create draft missing iso_time_utc
        draft = await create_action_draft(
            user_id=303,
            chat_id=404,
            action_type="schedule_reminder",
            payload={"text": "купити квитки"},
            missing_fields=["iso_time_utc"],
        )
        self.assertEqual(draft.status, DRAFT_STATUS_AWAITING_INFO)

        # User replies with free-form date
        reply_res = await apply_action_draft_reply(
            draft_id=draft.id,
            user_id=303,
            chat_id=404,
            reply_text="наступної пʼятниці о 16:30",
            timezone_name="Europe/Kyiv",
        )
        self.assertTrue(reply_res.payload.get("success"))
        self.assertEqual(reply_res.payload.get("status"), DRAFT_STATUS_PENDING_CONFIRMATION)
        self.assertEqual(reply_res.payload.get("missing_fields"), [])
        self.assertIn("16:30", reply_res.display_text)
        self.assertIn("купити квитки", reply_res.display_text)

        # Verify draft in DB
        updated_draft = await get_action_draft(draft.id, 303, 404)
        self.assertIsNotNone(updated_draft)
        self.assertEqual(updated_draft.status, DRAFT_STATUS_PENDING_CONFIRMATION)
        self.assertIn("T", updated_draft.payload.get("iso_time_utc", ""))


if __name__ == "__main__":
    unittest.main()
