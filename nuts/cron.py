import datetime
from dateutil.relativedelta import relativedelta
import calendar


class Cron():

    def __get_start_frequency(self, input: str, base: str = None) -> list[int]:
        try:
            [start, frequency] = input.split('/')
            if start == '*':
                start = base if base is not None else '0'
            return [int(start), int(frequency)]

        except Exception:
            raise Exception(f'Invalid expression {input}: Valid examples include: */int, int/int')

    def __get_week_structure(self, year: int, month: int) -> list[int]:
        start_of_month = calendar.monthrange(year, month)[0]
        week_structure = []
        while len(week_structure) < 7:
            if start_of_month < 8:
                week_structure.append(start_of_month)
                start_of_month += 1
            else:
                start_of_month = 1
        return week_structure

    def get_next_execution(self, schedule: str, now: datetime.datetime = None) -> datetime.datetime:
        if now is None:
            now = datetime.datetime.now(datetime.timezone.utc)
        try:
            [seconds, minutes, hours, day_of_week, month, day_of_month, year] = schedule.split(' ')
        except Exception:
            raise Exception('Expression must have 7 postions')

        if '?' not in [day_of_week, day_of_month]:
            raise Exception('Cannot specify both day-of-week and day-of-month')

        [next_seconds, inc_minute] = self.parse_seconds(seconds, now)
        if inc_minute:
            now = now + datetime.timedelta(minutes=1)
        [next_minutes, inc_hour] = self.parse_minutes(minutes, now)
        if inc_hour:
            now = now + datetime.timedelta(hours=1)
        [next_hours, inc_day] = self.parse_hours(hours, now)
        if inc_day:
            now = now + datetime.timedelta(days=1)
        if day_of_week == '?':
            [next_days, inc_month] = self.parse_day_of_month(day_of_month, now)

        else:
            [next_days, inc_month] = self.parse_day_of_week(day_of_week, now)

        if inc_month:
            now = now + relativedelta(months=1)
        [next_months, inc_year] = self.parse_months(month, now)
        if inc_year:
            now = now + relativedelta(years=1)

        [next_year, _] = self.parse_years(year, now)
        res = datetime.datetime(next_year, next_months, next_days, next_hours, next_minutes, next_seconds, tzinfo=datetime.timezone.utc)

        return res

    def parse_seconds(self, seconds: str, now: datetime.datetime) -> tuple[int, bool]:
        if seconds == '*':
            seconds = '0/1'

        if '/' not in seconds:
            start = int(seconds)
            frequency = 60
        else:
            [start, frequency] = self.__get_start_frequency(seconds)

        upcoming = [i for i in range(start, 60, frequency) if i > now.second]
        try:
            return [upcoming[0], False]
        except Exception:
            return [start, True]

    def parse_minutes(self, minutes: str, now: datetime.datetime) -> tuple[int, bool]:
        if minutes == '*':
            minutes = '0/1'

        if '/' not in minutes:
            start = int(minutes)
            frequency = 60
        else:
            [start, frequency] = self.__get_start_frequency(minutes)

        upcoming = [i for i in range(start, 60, frequency) if i >= now.minute]
        try:
            return [upcoming[0], False]
        except Exception:
            return [start, True]

    def parse_hours(self, hours: str, now: datetime.datetime) -> tuple[int, bool]:
        if hours == '*':
            hours = '0/1'

        if '/' not in hours:
            start = int(hours)
            frequency = 24
        else:
            [start, frequency] = self.__get_start_frequency(hours)

        upcoming = [i for i in range(start, 24, frequency) if i >= now.hour]
        try:
            return [upcoming[0], False]
        except Exception:
            return [start, True]

    def parse_day_of_month(self, days: str, now: datetime.datetime) -> tuple[int, bool]:
        end_of_month = calendar.monthrange(now.year, now.month)[1]
        if days == '*':
            days = '1/1'
        if '/' not in days:
            start = int(days)
            frequency = end_of_month
        else:
            [start, frequency] = self.__get_start_frequency(days, '1')

        upcoming = [i for i in range(start, end_of_month + 1, frequency) if i >= now.day]
        try:
            return [upcoming[0], False]
        except Exception:
            return [start, True]

    def __get_upcoming_weekdays(self, days: str, week_structure: list[int], now: datetime.datetime, frequency: int = None) -> list[int]:
        end_of_month = calendar.monthrange(now.year, now.month)[1] + 1
        month_days = list(range(1, end_of_month))
        weeks = [month_days[i * 7:(i+1) * 7] for i in range((len(month_days) + 7 - 1) // 7)]
        dow_idx = week_structure.index(int(days))

        upcoming = []

        if frequency:
            start = weeks[0][dow_idx]
            upcoming = [i for i in range(start, end_of_month, frequency) if i >= now.day]
        else:
            for w in weeks:
                try:
                    d = w[dow_idx]
                    if d >= now.day:
                        upcoming.append(d)
                except Exception:
                    pass
        return upcoming

    def parse_day_of_week(self, days: str, now: datetime.datetime) -> tuple[int, bool]:
        week_structure = self.__get_week_structure(now.year, now.month)
        if days == '*':
            days = f'{week_structure[0]}/1'

        if '/' not in days:
            start = int(days)

            upcoming = self.__get_upcoming_weekdays(start, week_structure, now)
            try:
                return [upcoming[0], False]
            except Exception:
                # Need to shift forward to the next month
                next_month = now + datetime.timedelta(days=calendar.monthrange(now.year, now.month)[1] - now.day + 1)
                week_structure = self.__get_week_structure(next_month.year, next_month.month)
                upcoming = self.__get_upcoming_weekdays(start, week_structure, next_month)

                return [upcoming[0], True]

        else:
            [start, frequency] = self.__get_start_frequency(days, '1')
            upcoming = self.__get_upcoming_weekdays(start, week_structure, now, frequency)
            try:
                return [upcoming[0], False]
            except Exception:
                # Need to shift forward to the next month
                now = now + datetime.timedelta(days=calendar.monthrange(now.year, now.month)[1] - now.day + 1)
                week_structure = self.__get_week_structure(now.year, now.month)
                upcoming = self.__get_upcoming_weekdays(start, week_structure, now, frequency)

                return [upcoming[0], True]

    def parse_months(self, months: str, now: datetime.datetime) -> tuple[int, bool]:
        if months == '*':
            months = '1/1'

        if '/' not in months:
            start = int(months)
            frequency = 12
        else:
            [start, frequency] = self.__get_start_frequency(months, '1')

        upcoming = [i for i in range(start, 13, frequency) if i >= now.month]
        try:
            return [upcoming[0], False]
        except Exception:
            return [start, True]

    def parse_years(self, years: str, now: datetime.datetime) -> tuple[int, bool]:
        if years == '*':
            years = '1970/1'

        if '/' not in years:
            start = int(years)
            frequency = 1
        else:
            [start, frequency] = self.__get_start_frequency(years, '1970')

        upcoming = [i for i in range(start, now.year + frequency, frequency) if i >= now.year]
        try:
            return [upcoming[0], False]
        except Exception:
            return [start, True]


if __name__ == '__main__':
    cron = Cron()
    cron.get_next_execution('2/4 * * * * ? *')
