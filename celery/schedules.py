# -*- coding: utf-8 -*-
"""
    celery.schedules
    ~~~~~~~~~~~~~~~~

    Schedules define the intervals at which periodic tasks
    should run.

"""
from __future__ import absolute_import

import re

from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta
from kombu.utils import cached_property

from . import current_app
from .utils import is_iterable
from .utils.timeutils import (
    timedelta_seconds, weekday, maybe_timedelta, remaining,
    humanize_seconds, timezone, maybe_make_aware
)
from .datastructures import AttributeDict

CRON_PATTERN_INVALID = """\
Invalid crontab pattern. Valid range is {min}-{max}. \
'{value}' was found.\
"""

CRON_INVALID_TYPE = """\
Argument cronspec needs to be of any of the following types: \
int, basestring, or an iterable type. {type!r} was given.\
"""


class ParseException(Exception):
    """Raised by crontab_parser when the input can't be parsed."""


class schedule(object):
    relative = False

    def __init__(self, run_every=None, relative=False, nowfun=None):
        self.run_every = maybe_timedelta(run_every)
        self.relative = relative
        self.nowfun = nowfun

    def now(self):
        return (self.nowfun or self.app.now)()

    def remaining_estimate(self, last_run_at):
        return remaining(last_run_at, self.run_every,
                         self.maybe_make_aware(self.now()), self.relative)

    def is_due(self, last_run_at):
        """Returns tuple of two items `(is_due, next_time_to_run)`,
        where next time to run is in seconds.

        e.g.

        * `(True, 20)`, means the task should be run now, and the next
            time to run is in 20 seconds.

        * `(False, 12)`, means the task should be run in 12 seconds.

        You can override this to decide the interval at runtime,
        but keep in mind the value of :setting:`CELERYBEAT_MAX_LOOP_INTERVAL`,
        which decides the maximum number of seconds celerybeat can sleep
        between re-checking the periodic task intervals.  So if you
        dynamically change the next run at value, and the max interval is
        set to 5 minutes, it will take 5 minutes for the change to take
        effect, so you may consider lowering the value of
        :setting:`CELERYBEAT_MAX_LOOP_INTERVAL` if responsiveness is of
        importance to you.

        .. admonition:: Scheduler max interval variance

            The default max loop interval may vary for different schedulers.
            For the default scheduler the value is 5 minutes, but for e.g.
            the django-celery database scheduler the value is 5 seconds.

        """
        last_run_at = self.maybe_make_aware(last_run_at)
        rem_delta = self.remaining_estimate(last_run_at)
        rem = timedelta_seconds(rem_delta)
        if rem == 0:
            return True, self.seconds
        return False, rem

    def maybe_make_aware(self, dt):
        if self.utc_enabled:
            return maybe_make_aware(dt, self.tz)
        return dt

    def __repr__(self):
        return '<freq: {0.human_seconds}>'.format(self)

    def __eq__(self, other):
        if isinstance(other, schedule):
            return self.run_every == other.run_every
        return self.run_every == other

    @property
    def seconds(self):
        return timedelta_seconds(self.run_every)

    @property
    def human_seconds(self):
        return humanize_seconds(self.seconds)

    @cached_property
    def app(self):
        return current_app._get_current_object()

    @cached_property
    def tz(self):
        return timezone.get_timezone(self.app.conf.CELERY_TIMEZONE)

    @cached_property
    def utc_enabled(self):
        return self.app.conf.CELERY_ENABLE_UTC

    @cached_property
    def to_local(self):
        return (timezone.to_local if self.utc_enabled
                                  else timezone.to_local_fallback)


class crontab_parser(object):
    """Parser for crontab expressions. Any expression of the form 'groups'
    (see BNF grammar below) is accepted and expanded to a set of numbers.
    These numbers represent the units of time that the crontab needs to
    run on::

        digit   :: '0'..'9'
        dow     :: 'a'..'z'
        number  :: digit+ | dow+
        steps   :: number
        range   :: number ( '-' number ) ?
        numspec :: '*' | range
        expr    :: numspec ( '/' steps ) ?
        groups  :: expr ( ',' expr ) *

    The parser is a general purpose one, useful for parsing hours, minutes and
    day_of_week expressions.  Example usage::

        >>> minutes = crontab_parser(60).parse('*/15')
        [0, 15, 30, 45]
        >>> hours = crontab_parser(24).parse('*/4')
        [0, 4, 8, 12, 16, 20]
        >>> day_of_week = crontab_parser(7).parse('*')
        [0, 1, 2, 3, 4, 5, 6]

    It can also parse day_of_month and month_of_year expressions if initialized
    with an minimum of 1.  Example usage::

        >>> days_of_month = crontab_parser(31, 1).parse('*/3')
        [1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 31]
        >>> months_of_year = crontab_parser(12, 1).parse('*/2')
        [1, 3, 5, 7, 9, 11]
        >>> months_of_year = crontab_parser(12, 1).parse('2-12/2')
        [2, 4, 6, 8, 10, 12]

    The maximum possible expanded value returned is found by the formula::

        max_ + min_ - 1

    """
    ParseException = ParseException

    _range = r'(\w+?)-(\w+)'
    _steps = r'/(\w+)?'
    _star = r'\*'

    def __init__(self, max_=60, min_=0):
        self.max_ = max_
        self.min_ = min_
        self.pats = (
                (re.compile(self._range + self._steps), self._range_steps),
                (re.compile(self._range), self._expand_range),
                (re.compile(self._star + self._steps), self._star_steps),
                (re.compile('^' + self._star + '$'), self._expand_star))

    def parse(self, spec):
        acc = set()
        for part in spec.split(','):
            if not part:
                raise self.ParseException('empty part')
            acc |= set(self._parse_part(part))
        return acc

    def _parse_part(self, part):
        for regex, handler in self.pats:
            m = regex.match(part)
            if m:
                return handler(m.groups())
        return self._expand_range((part, ))

    def _expand_range(self, toks):
        fr = self._expand_number(toks[0])
        if len(toks) > 1:
            to = self._expand_number(toks[1])
            return range(fr, min(to + 1, self.max_ + 1))
        return [fr]

    def _range_steps(self, toks):
        if len(toks) != 3 or not toks[2]:
            raise self.ParseException('empty filter')
        return self._expand_range(toks[:2])[::int(toks[2])]

    def _star_steps(self, toks):
        if not toks or not toks[0]:
            raise self.ParseException('empty filter')
        return self._expand_star()[::int(toks[0])]

    def _expand_star(self, *args):
        return range(self.min_, self.max_ + self.min_)

    def _expand_number(self, s):
        if isinstance(s, basestring) and s[0] == '-':
            raise self.ParseException('negative numbers not supported')
        try:
            i = int(s)
        except ValueError:
            try:
                i = weekday(s)
            except KeyError:
                raise ValueError('Invalid weekday literal {0!r}.'.format(s))

        if i < self.min_:
            raise ValueError(
                'Invalid beginning range: {0} < {1}.'.format(i, self.min_))
        return i


class crontab(schedule):
    """A crontab can be used as the `run_every` value of a
    :class:`PeriodicTask` to add cron-like scheduling.

    Like a :manpage:`cron` job, you can specify units of time of when
    you would like the task to execute. It is a reasonably complete
    implementation of cron's features, so it should provide a fair
    degree of scheduling needs.

    You can specify a minute, an hour, a day of the week, a day of the
    month, and/or a month in the year in any of the following formats:

    .. attribute:: minute

        - A (list of) integers from 0-59 that represent the minutes of
          an hour of when execution should occur; or
        - A string representing a crontab pattern.  This may get pretty
          advanced, like `minute='*/15'` (for every quarter) or
          `minute='1,13,30-45,50-59/2'`.

    .. attribute:: hour

        - A (list of) integers from 0-23 that represent the hours of
          a day of when execution should occur; or
        - A string representing a crontab pattern.  This may get pretty
          advanced, like `hour='*/3'` (for every three hours) or
          `hour='0,8-17/2'` (at midnight, and every two hours during
          office hours).

    .. attribute:: day_of_week

        - A (list of) integers from 0-6, where Sunday = 0 and Saturday =
          6, that represent the days of a week that execution should
          occur.
        - A string representing a crontab pattern.  This may get pretty
          advanced, like `day_of_week='mon-fri'` (for weekdays only).
          (Beware that `day_of_week='*/2'` does not literally mean
          'every two days', but 'every day that is divisible by two'!)

    .. attribute:: day_of_month

        - A (list of) integers from 1-31 that represents the days of the
          month that execution should occur.
        - A string representing a crontab pattern.  This may get pretty
          advanced, such as `day_of_month='2-30/3'` (for every even
          numbered day) or `day_of_month='1-7,15-21'` (for the first and
          third weeks of the month).

    .. attribute:: month_of_year

        - A (list of) integers from 1-12 that represents the months of
          the year during which execution can occur.
        - A string representing a crontab pattern.  This may get pretty
          advanced, such as `month_of_year='*/3'` (for the first month
          of every quarter) or `month_of_year='2-12/2'` (for every even
          numbered month).

    It is important to realize that any day on which execution should
    occur must be represented by entries in all three of the day and
    month attributes.  For example, if `day_of_week` is 0 and `day_of_month`
    is every seventh day, only months that begin on Sunday and are also
    in the `month_of_year` attribute will have execution events.  Or,
    `day_of_week` is 1 and `day_of_month` is '1-7,15-21' means every
    first and third monday of every month present in `month_of_year`.

    """

    @staticmethod
    def _expand_cronspec(cronspec, max_, min_=0):
        """Takes the given cronspec argument in one of the forms::

            int         (like 7)
            basestring  (like '3-5,*/15', '*', or 'monday')
            set         (like set([0,15,30,45]))
            list        (like [8-17])

        And convert it to an (expanded) set representing all time unit
        values on which the crontab triggers.  Only in case of the base
        type being 'basestring', parsing occurs.  (It is fast and
        happens only once for each crontab instance, so there is no
        significant performance overhead involved.)

        For the other base types, merely Python type conversions happen.

        The argument `max_` is needed to determine the expansion of '*'
        and ranges.
        The argument `min_` is needed to determine the expansion of '*'
        and ranges for 1-based cronspecs, such as day of month or month
        of year. The default is sufficient for minute, hour, and day of
        week.

        """
        if isinstance(cronspec, int):
            result = set([cronspec])
        elif isinstance(cronspec, basestring):
            result = crontab_parser(max_, min_).parse(cronspec)
        elif isinstance(cronspec, set):
            result = cronspec
        elif is_iterable(cronspec):
            result = set(cronspec)
        else:
            raise TypeError(CRON_INVALID_TYPE.format(type=type(cronspec)))

        # assure the result does not preceed the min or exceed the max
        for number in result:
            if number >= max_ + min_ or number < min_:
                raise ValueError(CRON_PATTERN_INVALID.format(
                    min=min_, max=max_ - 1 + min_, value=number))
        return result

    def _delta_to_next(self, last_run_at, next_hour, next_minute):
        """
        Takes a datetime of last run, next minute and hour, and
        returns a relativedelta for the next scheduled day and time.
        Only called when day_of_month and/or month_of_year cronspec
        is specified to further limit scheduled task execution.
        """
        from bisect import bisect, bisect_left

        datedata = AttributeDict(year=last_run_at.year)
        days_of_month = sorted(self.day_of_month)
        months_of_year = sorted(self.month_of_year)

        def day_out_of_range(year, month, day):
            try:
                datetime(year=year, month=month, day=day)
            except ValueError:
                return True
            return False

        def roll_over():
            while 1:
                flag = (datedata.dom == len(days_of_month) or
                            day_out_of_range(datedata.year,
                                             months_of_year[datedata.moy],
                                             days_of_month[datedata.dom]))
                if flag:
                    datedata.dom = 0
                    datedata.moy += 1
                    if datedata.moy == len(months_of_year):
                        datedata.moy = 0
                        datedata.year += 1
                else:
                    break

        if last_run_at.month in self.month_of_year:
            datedata.dom = bisect(days_of_month, last_run_at.day)
            datedata.moy = bisect_left(months_of_year, last_run_at.month)
        else:
            datedata.dom = 0
            datedata.moy = bisect(months_of_year, last_run_at.month)
        roll_over()

        while not (datetime(year=datedata.year,
                            month=months_of_year[datedata.moy],
                            day=days_of_month[datedata.dom]
                           ).isoweekday() % 7
                  ) in self.day_of_week:
            datedata.dom += 1
            roll_over()

        return relativedelta(year=datedata.year,
                             month=months_of_year[datedata.moy],
                             day=days_of_month[datedata.dom],
                             hour=next_hour,
                             minute=next_minute,
                             second=0,
                             microsecond=0)

    def __init__(self, minute='*', hour='*', day_of_week='*',
            day_of_month='*', month_of_year='*', nowfun=None):
        self._orig_minute = minute
        self._orig_hour = hour
        self._orig_day_of_week = day_of_week
        self._orig_day_of_month = day_of_month
        self._orig_month_of_year = month_of_year
        self.hour = self._expand_cronspec(hour, 24)
        self.minute = self._expand_cronspec(minute, 60)
        self.day_of_week = self._expand_cronspec(day_of_week, 7)
        self.day_of_month = self._expand_cronspec(day_of_month, 31, 1)
        self.month_of_year = self._expand_cronspec(month_of_year, 12, 1)
        self.nowfun = nowfun

    def now(self):
        return (self.nowfun or self.app.now)()

    def __repr__(self):
        return ('<crontab: %s %s %s %s %s (m/h/d/dM/MY)>' %
                                            (self._orig_minute or '*',
                                             self._orig_hour or '*',
                                             self._orig_day_of_week or '*',
                                             self._orig_day_of_month or '*',
                                             self._orig_month_of_year or '*'))

    def __reduce__(self):
        return (self.__class__, (self._orig_minute,
                                 self._orig_hour,
                                 self._orig_day_of_week,
                                 self._orig_day_of_month,
                                 self._orig_month_of_year), None)

    def remaining_estimate(self, last_run_at, tz=None):
        """Returns when the periodic task should run next as a timedelta."""
        tz = tz or self.tz
        last_run_at = self.maybe_make_aware(last_run_at)
        dow_num = last_run_at.isoweekday() % 7  # Sunday is day 0, not day 7

        execute_this_date = (last_run_at.month in self.month_of_year and
                                last_run_at.day in self.day_of_month and
                                    dow_num in self.day_of_week)

        execute_this_hour = (execute_this_date and
                                last_run_at.hour in self.hour and
                                    last_run_at.minute < max(self.minute))

        if execute_this_hour:
            next_minute = min(minute for minute in self.minute
                                        if minute > last_run_at.minute)
            delta = relativedelta(minute=next_minute,
                                  second=0,
                                  microsecond=0)
        else:
            next_minute = min(self.minute)
            execute_today = (execute_this_date and
                                last_run_at.hour < max(self.hour))

            if execute_today:
                next_hour = min(hour for hour in self.hour
                                        if hour > last_run_at.hour)
                delta = relativedelta(hour=next_hour,
                                      minute=next_minute,
                                      second=0,
                                      microsecond=0)
            else:
                next_hour = min(self.hour)
                all_dom_moy = (self._orig_day_of_month == '*' and
                                  self._orig_month_of_year == '*')
                if all_dom_moy:
                    next_day = min([day for day in self.day_of_week
                                        if day > dow_num] or
                                self.day_of_week)
                    add_week = next_day == dow_num

                    delta = relativedelta(weeks=add_week and 1 or 0,
                                          weekday=(next_day - 1) % 7,
                                          hour=next_hour,
                                          minute=next_minute,
                                          second=0,
                                          microsecond=0)
                else:
                    delta = self._delta_to_next(last_run_at,
                                                next_hour, next_minute)

        return remaining(self.to_local(last_run_at, tz),
                         delta, self.to_local(self.now(), tz))

    def is_due(self, last_run_at):
        """Returns tuple of two items `(is_due, next_time_to_run)`,
        where next time to run is in seconds.

        See :meth:`celery.schedules.schedule.is_due` for more information.

        """
        rem_delta = self.remaining_estimate(last_run_at)
        rem = timedelta_seconds(rem_delta)
        due = rem == 0
        if due:
            rem_delta = self.remaining_estimate(self.now())
            rem = timedelta_seconds(rem_delta)
        return due, rem

    def __eq__(self, other):
        if isinstance(other, crontab):
            return (other.month_of_year == self.month_of_year and
                    other.day_of_month == self.day_of_month and
                    other.day_of_week == self.day_of_week and
                    other.hour == self.hour and
                    other.minute == self.minute)
        return other is self


def maybe_schedule(s, relative=False):
    if isinstance(s, int):
        s = timedelta(seconds=s)
    if isinstance(s, timedelta):
        return schedule(s, relative)
    return s
