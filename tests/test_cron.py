import datetime
from ..nuts.cron import Cron

cron = Cron()


def test_get_next_execution_bad_formats():
    try:
        cron.get_next_execution('* * *')
    except Exception as ex:
        assert ex.args[0] == 'Expression must have 7 postions'

    try:
        cron.get_next_execution('* * * * * * *')
    except Exception as ex:
        assert ex.args[0] == 'Cannot specify both day-of-week and day-of-month'


def test_get_next_execution():
    now = datetime.datetime(2025, 1, 9, 0, 0, 0)
    res = cron.get_next_execution('2 * * * * ? *', now)

    assert res == datetime.datetime(2025, 1, 9, 0, 0, 2, tzinfo=datetime.timezone.utc)

    now = datetime.datetime(2025, 1, 9, 0, 0, 2)
    res = cron.get_next_execution('2 * * * * ? *', now)

    assert res == datetime.datetime(2025, 1, 9, 0, 1, 2, tzinfo=datetime.timezone.utc)

    now = datetime.datetime(2025, 1, 9, 0, 0, 0)
    res = cron.get_next_execution('* * * * * ? *', now)

    assert res == datetime.datetime(2025, 1, 9, 0, 0, 1, tzinfo=datetime.timezone.utc)

    now = datetime.datetime(2025, 1, 9, 0, 0, 0)
    res = cron.get_next_execution('2 1 * * * ? *', now)

    assert res == datetime.datetime(2025, 1, 9, 0, 1, 2, tzinfo=datetime.timezone.utc)

    now = datetime.datetime(2025, 1, 9, 0, 59, 0)
    res = cron.get_next_execution('2 1 * * * ? *', now)

    assert res == datetime.datetime(2025, 1, 9, 1, 1, 2, tzinfo=datetime.timezone.utc)

    now = datetime.datetime(2025, 1, 9, 23, 0, 0)
    res = cron.get_next_execution('0 0 1 * * ? *', now)

    assert res == datetime.datetime(2025, 1, 10, 1, 0, 0, tzinfo=datetime.timezone.utc)

    now = datetime.datetime(2025, 1, 9, 23, 0, 0)
    res = cron.get_next_execution('* * 1/1 * * ? *', now)

    assert res == datetime.datetime(2025, 1, 9, 23, 0, 1, tzinfo=datetime.timezone.utc)


def test_parse_seconds():

    now = datetime.datetime(2025, 1, 9, 15, 2, 0)
    res = cron.parse_seconds('2', now)
    assert res[0] == 2
    assert res[1] is False

    now = datetime.datetime(2025, 1, 9, 15, 2, 59)
    res = cron.parse_seconds('2', now)
    assert res[0] == 2
    assert res[1] is True

    now = datetime.datetime(2025, 1, 9, 15, 2, 0)
    res = cron.parse_seconds('2/4', now)
    assert res[0] == 2
    assert res[1] is False

    now = datetime.datetime(2025, 1, 9, 15, 2, 2)
    res = cron.parse_seconds('2/4', now)
    assert res[0] == 6

    now = datetime.datetime(2025, 1, 9, 15, 2, 59)
    res = cron.parse_seconds('2/4', now)
    assert res[0] == 2
    assert res[1] is True

    now = datetime.datetime(2025, 1, 9, 15, 2, 22)
    res = cron.parse_seconds('*/4', now)
    assert res[0] == 24
    assert res[1] is False

    now = datetime.datetime(2025, 1, 9, 15, 2, 59)
    res = cron.parse_seconds('*/4', now)
    assert res[0] == 0
    assert res[1] is True


def test_parse_minutes():

    now = datetime.datetime(2025, 1, 9, 15, 0, 0)
    res = cron.parse_minutes('2', now)
    assert res[0] == 2
    assert res[1] is False

    now = datetime.datetime(2025, 1, 9, 15, 0, 0)
    res = cron.parse_minutes('2/4', now)
    assert res[0] == 2
    assert res[1] is False

    now = datetime.datetime(2025, 1, 9, 15, 2, 2)
    res = cron.parse_minutes('2/4', now)
    assert res[0] == 2

    now = datetime.datetime(2025, 1, 9, 15, 59, 59)
    res = cron.parse_minutes('2/4', now)
    assert res[0] == 2
    assert res[1] is True

    now = datetime.datetime(2025, 1, 9, 15, 22, 22)
    res = cron.parse_minutes('*/4', now)
    assert res[0] == 24
    assert res[1] is False

    now = datetime.datetime(2025, 1, 9, 15, 59, 0)
    res = cron.parse_minutes('*/4', now)
    assert res[0] == 0
    assert res[1] is True


def test_parse_hours():

    now = datetime.datetime(2025, 1, 9, 0, 0, 0)
    res = cron.parse_hours('2', now)
    assert res[0] == 2
    assert res[1] is False

    now = datetime.datetime(2025, 1, 9, 23, 0, 0)
    res = cron.parse_hours('2', now)
    assert res[0] == 2
    assert res[1] is True

    now = datetime.datetime(2025, 1, 9, 0, 0, 0)
    res = cron.parse_hours('2/4', now)
    assert res[0] == 2
    assert res[1] is False

    now = datetime.datetime(2025, 1, 9, 2, 0, 0)
    res = cron.parse_hours('2/4', now)
    assert res[0] == 2
    assert res[1] is False

    now = datetime.datetime(2025, 1, 9, 23, 0, 0)
    res = cron.parse_hours('2/4', now)
    assert res[0] == 2
    assert res[1] is True

    now = datetime.datetime(2025, 1, 9, 23, 0, 0)
    res = cron.parse_hours('*/4', now)
    assert res[0] == 0
    assert res[1] is True

    now = datetime.datetime(2025, 1, 9, 4, 0, 0)
    res = cron.parse_hours('*/4', now)
    assert res[0] == 4
    assert res[1] is False


def test_parse_day_of_month():

    now = datetime.datetime(2025, 1, 1, 0, 0, 0)
    res = cron.parse_day_of_month('2', now)
    assert res[0] == 2
    assert res[1] is False

    now = datetime.datetime(2025, 1, 1, 0, 0, 0)
    res = cron.parse_day_of_month('2/4', now)
    assert res[0] == 2
    assert res[1] is False

    now = datetime.datetime(2025, 1, 2, 0, 0, 0)
    res = cron.parse_day_of_month('2/4', now)
    assert res[0] == 2
    assert res[1] is False

    now = datetime.datetime(2025, 1, 31, 0, 0, 0)
    res = cron.parse_day_of_month('2/4', now)
    assert res[0] == 2
    assert res[1] is True

    now = datetime.datetime(2025, 1, 1, 0, 0, 0)
    res = cron.parse_day_of_month('1/1', now)
    assert res[0] == 1
    assert res[1] is False

    now = datetime.datetime(2025, 1, 1, 0, 0, 0)
    res = cron.parse_day_of_month('*/4', now)
    assert res[0] == 1
    assert res[1] is False

    now = datetime.datetime(2025, 1, 4, 0, 0, 0)
    res = cron.parse_day_of_month('*/4', now)
    assert res[0] == 5
    assert res[1] is False

    now = datetime.datetime(2025, 1, 5, 0, 0, 0)
    res = cron.parse_day_of_month('*/4', now)
    assert res[0] == 5
    assert res[1] is False


def test_parse_day_of_week():

    now = datetime.datetime(2025, 1, 1, 0, 0, 0)
    res = cron.parse_day_of_week('2', now)
    assert res[0] == 1
    assert res[1] is False

    now = datetime.datetime(2025, 1, 29, 0, 0, 0)
    res = cron.parse_day_of_week('2', now)
    assert res[0] == 29
    assert res[1] is False

    now = datetime.datetime(2025, 1, 30, 0, 0, 0)
    res = cron.parse_day_of_week('2', now)
    assert res[0] == 5
    assert res[1] is True

    now = datetime.datetime(2025, 1, 1, 0, 0, 0)
    res = cron.parse_day_of_week('2/4', now)
    assert res[0] == 1

    now = datetime.datetime(2025, 1, 7, 0, 0, 0)
    res = cron.parse_day_of_week('2/4', now)
    assert res[0] == 9

    now = datetime.datetime(2025, 1, 8, 0, 0, 0)
    res = cron.parse_day_of_week('2/4', now)
    assert res[0] == 9

    now = datetime.datetime(2025, 1, 29, 0, 0, 0)
    res = cron.parse_day_of_week('2/4', now)
    assert res[0] == 29
    assert res[1] is False

    now = datetime.datetime(2025, 1, 31, 0, 0, 0)
    res = cron.parse_day_of_week('2/5', now)
    assert res[0] == 31
    assert res[1] is False

    now = datetime.datetime(2025, 1, 1, 0, 0, 0)
    res = cron.parse_day_of_week('*', now)
    assert res[0] == 1
    assert res[1] is False


def test_parse_months():

    now = datetime.datetime(2025, 1, 1, 0, 0, 0)
    res = cron.parse_months('1', now)
    assert res[0] == 1
    assert res[1] is False

    now = datetime.datetime(2025, 12, 1, 0, 0, 0)
    res = cron.parse_months('1', now)
    assert res[0] == 1
    assert res[1] is True

    now = datetime.datetime(2025, 1, 1, 0, 0, 0)
    res = cron.parse_months('2/4', now)
    assert res[0] == 2
    assert res[1] is False


def test_parse_years():
    now = datetime.datetime(2025, 1, 1, 0, 0, 0)
    res = cron.parse_years('2025', now)
    assert res[0] == 2025
    assert res[1] is False

    now = datetime.datetime(2025, 1, 1, 0, 0, 0)
    res = cron.parse_years('2024', now)
    assert res[0] == 2025
    assert res[1] is False

    now = datetime.datetime(2025, 1, 1, 0, 0, 0)
    res = cron.parse_years('2024/2', now)
    assert res[0] == 2026
    assert res[1] is False

    now = datetime.datetime(2025, 1, 1, 0, 0, 0)
    res = cron.parse_years('*/2', now)
    assert res[0] == 2026
    assert res[1] is False
