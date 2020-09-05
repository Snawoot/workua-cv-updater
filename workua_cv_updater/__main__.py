#!/usr/bin/env python3

import logging
import argparse
import enum
import os
import os.path
import sqlite3
import signal
from time import sleep, time, ctime, localtime, strftime
from random import randrange, random
import collections
from heapq import merge
from contextlib import contextmanager
import json

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (TimeoutException,
                                        StaleElementReferenceException,
                                        NoSuchElementException,
                                        ElementClickInterceptedException,
                                        WebDriverException)
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.utils import ChromeType

RESUME_LIST_URL = "https://www.work.ua/ru/jobseeker/my/resumes/"
RESUME_LIST_URL_PATTERN = r"^https://www\.work\.ua/(ru/)?jobseeker/my/resumes/?$"
LOGIN_URL = "https://www.work.ua/jobseeker/login/"
POST_LOGIN_URL_PATTERN = r"^https://www\.work\.ua/(ru/)?jobseeker/my/?$"
CREATE_URL_PATTERN = r"^https://www.work.ua/(ru/)?jobseeker/my/resumes/create/?(\?.*)?$"
UPDATE_BUTTON_XPATH = "//a[contains(@href, '/update/expire_date')]"
CREATE_BUTTON_XPATH = "//a[contains(@href, '/jobseeker/my/resumes/create')]"
UPDATE_INTERVAL = 7 * 24 * 3600
UPDATE_INTERVAL_MIN_DRIFT = 10
UPDATE_INTERVAL_MAX_DRIFT = 60
SESSION_REFRESH_INTERVAL = 1 * 3600
SESSION_REFRESH_INTERVAL_MIN_DRIFT = 10
SESSION_REFRESH_INTERVAL_MAX_DRIFT = 60
MANUAL_LOGIN_TIMEOUT = 3600
REFRESH_WAIT = 10

DB_INIT = [
    "CREATE TABLE IF NOT EXISTS update_ts (\n"
    "name TEXT PRIMARY KEY,\n"
    "value REAL NOT NULL DEFAULT 0)\n"
]

def wall_clock_wait(when, precision=1.):
    """ Sleep variation which is doesn't increases
    sleep duration when computer enters suspend/hybernation
    """
    while time() < when:
        sleep(precision)

def setup_logger(name, verbosity):
    logger = logging.getLogger(name)
    logger.setLevel(verbosity)
    handler = logging.StreamHandler()
    handler.setLevel(verbosity)
    handler.setFormatter(logging.Formatter("%(asctime)s "
                                           "%(levelname)-8s "
                                           "%(name)s: %(message)s",
                                           "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    return logger

class LogLevel(enum.IntEnum):
    debug = logging.DEBUG
    info = logging.INFO
    warn = logging.WARN
    error = logging.ERROR
    fatal = logging.FATAL
    crit = logging.CRITICAL

    def __str__(self):
        return self.name

class Command(enum.Enum):
    login = 1
    update = 2

    def __str__(self):
        return self.name

class BrowserType(enum.Enum):
    chrome = ChromeType.GOOGLE
    chromium = ChromeType.CHROMIUM

    def __str__(self):
        return self.name

class ScheduledEvent(enum.Enum):
    REFRESH = 1
    UPDATE = 2

ScheduleEntry = collections.namedtuple('ScheduleEntry', ('when', 'what'))

def update(browser, timeout):
    logger = logging.getLogger("UPDATE")
    browser.get(RESUME_LIST_URL)
    visited_urls = set()
    while True:
        for elem in browser.find_elements_by_xpath(UPDATE_BUTTON_XPATH):
            href = elem.get_attribute("href")
            logger.debug("Update link href = %s", repr(href))
            if href in visited_urls:
                continue
            sleep(1 + 2 * random())
            elem.click()
            visited_urls.add(href)
            logger.debug("Clicked!")
            WebDriverWait(browser, timeout).until(
                EC.staleness_of(elem)
            )
            WebDriverWait(browser, timeout).until(
                EC.url_matches(RESUME_LIST_URL_PATTERN)
            )
            break
        else:
            break
    logger.info('Updated!')

def login(browser, timeout):
    logger = logging.getLogger("LOGIN")
    browser.get(LOGIN_URL)
    WebDriverWait(browser, timeout).until(
        EC.url_matches(POST_LOGIN_URL_PATTERN)
    )
    sleep(REFRESH_WAIT)
    logger.info('Successfully logged in!')

def refresh(browser, timeout):
    logger = logging.getLogger("REFRESH")
    browser.get(RESUME_LIST_URL)
    elem = WebDriverWait(browser, timeout).until(
        EC.visibility_of_element_located((By.XPATH, CREATE_BUTTON_XPATH))
    )
    elem.click()
    WebDriverWait(browser, timeout).until(
        EC.url_matches(CREATE_URL_PATTERN)
    )
    sleep(REFRESH_WAIT)
    logger.info('Session refreshed')

def parse_args():
    def check_loglevel(arg):
        try:
            return LogLevel[arg]
        except (IndexError, KeyError):
            raise argparse.ArgumentTypeError("%s is not valid loglevel" % (repr(arg),))

    def check_command(arg):
        try:
            return Command[arg]
        except (IndexError, KeyError):
            raise argparse.ArgumentTypeError("%s is not valid command" % (repr(arg),))

    def check_browser_type(arg):
        try:
            return BrowserType[arg]
        except (IndexError, KeyError):
            raise argparse.ArgumentTypeError("%s is not valid browser type" % (repr(arg),))

    def check_positive_float(arg):
        def fail():
            raise argparse.ArgumentTypeError("%s is not valid positive float" % (repr(arg),))
        try:
            fvalue = float(arg)
        except ValueError:
            fail()
        if fvalue <= 0:
            fail()
        return fvalue

    parser = argparse.ArgumentParser(
        description="Python script to update your CV",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("-t", "--timeout",
                        help="webdriver wait timeout",
                        type=check_positive_float,
                        default=10.)
    parser.add_argument("-b", "--browser",
                        help="browser type",
                        type=check_browser_type,
                        choices=BrowserType,
                        default=BrowserType.chromium)
    parser.add_argument("-v", "--verbosity",
                        help="logging verbosity",
                        type=check_loglevel,
                        choices=LogLevel,
                        default=LogLevel.info)
    parser.add_argument("cmd", help="command",
                        type=check_command,
                        choices=Command)
    parser.add_argument("-d", "--data-dir",
                        default=os.path.join(os.path.expanduser("~"),
                                             '.config',
                                             'workua-cv-updater'),
                        help="application datadir location",
                        metavar="FILE")
    return parser.parse_args()

class BrowserFactory:
    def __init__(self, profile_dir, screenshot_dir, browser_type, headless=True):
        chrome_options = Options()
        # option below causes webdriver process remaining in memory
        # chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('user-data-dir=' + profile_dir)
        chrome_options.add_argument('window-size=1920,1055')
        if headless:
            chrome_options.add_argument('--headless')
        self._options = chrome_options
        self._driver = ChromeDriverManager(chrome_type=browser_type).install()
        self._screenshot_dir = screenshot_dir

    def new(self):
        return webdriver.Chrome(
            self._driver,
            options=self._options)

    @property
    def screenshot_dir(self):
        return self._screenshot_dir

class UpdateTracker:
    def __init__(self, dbpath):
        conn = sqlite3.connect(dbpath)
        cur = conn.cursor()
        try:
            for q in DB_INIT:
                cur.execute(q)
            conn.commit()
            cur.execute("SELECT 1 FROM update_ts WHERE name = ?", ("last",))
            if cur.fetchone() is None:
                cur.execute("INSERT INTO update_ts (name, value) VALUES (?,?)",
                            ("last", 0.))
                conn.commit()
            cur.execute("SELECT 1 FROM update_ts WHERE name = ?", ("login",))
            if cur.fetchone() is None:
                cur.execute("INSERT INTO update_ts (name, value) VALUES (?,?)",
                            ("login", 0.))
                conn.commit()
        finally:
            cur.close()
        self._conn = conn

    def last_update(self):
        cur = self._conn.cursor()
        try:
            cur.execute("SELECT value FROM update_ts WHERE name = ?",
                        ("last",))
            return cur.fetchone()[0]
        finally:
            cur.close()

    def last_login(self):
        cur = self._conn.cursor()
        try:
            cur.execute("SELECT value FROM update_ts WHERE name = ?",
                        ("login",))
            return cur.fetchone()[0]
        finally:
            cur.close()

    def update(self, ts):
        c = self._conn
        with c:
            c.execute("UPDATE update_ts SET value = ? WHERE name = ? AND value < ?",
                      (float(ts), "last", float(ts)))

    def login(self, ts):
        c = self._conn
        with c:
            c.execute("UPDATE update_ts SET value = ? WHERE name = ? AND value < ?",
                      (float(ts), "login", float(ts)))

    def close(self):
        self._conn.close()
        self._conn = None

def random_interval(base, min_drift, max_drift):
    return base + min_drift + random() * (max_drift - min_drift)

class Scheduler:
    def __init__(self, last_login, last_update):
        self._it = self._iter_events(last_login, last_update)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    @staticmethod
    def _event_stream(token, last_occured, base, min_drift, max_drift):
        t = max(last_occured + random_interval(base, min_drift, max_drift), time())
        yield ScheduleEntry(when=t, what=token)
        while True:
            t += random_interval(base, min_drift, max_drift)
            yield ScheduleEntry(when=t, what=token)

    @staticmethod
    def _iter_events(last_login, last_update):
        return merge(
            Scheduler._event_stream(ScheduledEvent.REFRESH,
                                    last_login,
                                    SESSION_REFRESH_INTERVAL,
                                    SESSION_REFRESH_INTERVAL_MIN_DRIFT,
                                    SESSION_REFRESH_INTERVAL_MAX_DRIFT),
            Scheduler._event_stream(ScheduledEvent.UPDATE,
                                    last_update,
                                    UPDATE_INTERVAL,
                                    UPDATE_INTERVAL_MIN_DRIFT,
                                    UPDATE_INTERVAL_MAX_DRIFT),
            key=lambda ev: ev.when
        )

@contextmanager
def managed_browser(browser_factory):
    logger = logging.getLogger("GUARD")
    browser = browser_factory.new()
    try:
        yield browser
    except WebDriverException as exc:
        logger.warning("WebDriver exception occured: %s. Saving essential data...", str(exc))
        logger.warning("Current URL: %s", browser.current_url)
        logger.warning("Cookies: \n%s", json.dumps(browser.get_cookies(), indent=4))
        ss_filename = strftime("err-%Y-%m-%d-%H-%M-%S.png", localtime())
        ss_path = os.path.join(browser_factory.screenshot_dir, ss_filename)
        browser.save_screenshot(ss_path)
        logger.warning("Screenshot saved to %s", ss_path)
        raise
    else:
        logger.debug("Current URL: %s", browser.current_url)
        logger.debug("Cookies: \n%s", json.dumps(browser.get_cookies(), indent=4))
    finally:
        browser.quit()

def update_loop(browser_factory, tracker, timeout):
    logger = logging.getLogger("EVLOOP")
    last_update = tracker.last_update()
    last_login = tracker.last_login()
    logger.info("Starting scheduler. "
                "Last update @ %.3f (%s); last refresh @ %.3f (%s).",
                last_update, ctime(last_update),
                last_login, ctime(last_login))
    for ev in Scheduler(last_login, last_update):
        logger.info("Next event is %s @ %.3f (%s)",
                    ev.what.name, ev.when, ctime(ev.when))
        wall_clock_wait(ev.when)
        try:
            if ev.what is ScheduledEvent.REFRESH:
                logger.info("Refreshing session now!")
                with managed_browser(browser_factory) as browser:
                    refresh(browser, timeout)
                tracker.login(time())
            elif ev.what is ScheduledEvent.UPDATE:
                logger.info("Updating CVs now!")
                with managed_browser(browser_factory) as browser:
                    try:
                        update(browser, timeout)
                    except WebDriverException as exc:
                        raise
                tracker.update(time())
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logger.exception("Event %s handling failed: %s", ev.what.name, str(exc))

def sig_handler(signum, frame):
    raise KeyboardInterrupt

def main():
    args = parse_args()
    mainlogger = setup_logger("MAIN", args.verbosity)
    setup_logger("UPDATE", args.verbosity)
    setup_logger("LOGIN", args.verbosity)
    setup_logger("REFRESH", args.verbosity)
    setup_logger("EVLOOP", args.verbosity)
    setup_logger("GUARD", args.verbosity)

    screenshot_dir = os.path.join(args.data_dir, "screenshots")
    os.makedirs(screenshot_dir, mode=0o700, exist_ok=True)
    profile_dir = os.path.join(args.data_dir, "profile")
    browser_factory = BrowserFactory(profile_dir,
                                     screenshot_dir,
                                     args.browser.value,
                                     args.cmd is Command.update)
    db_path = os.path.join(args.data_dir, 'updater.db')
    tracker = UpdateTracker(db_path)
    signal.signal(signal.SIGTERM, sig_handler)

    try:
        if args.cmd is Command.login:
            mainlogger.info("Login mode. Please enter your credentials in opened "
                            "browser window.")
            try:
                with managed_browser(browser_factory) as browser:
                    login(browser, MANUAL_LOGIN_TIMEOUT)
                tracker.login(time())
            except KeyboardInterrupt:
                mainlogger.warning("Interrupted!")
        elif args.cmd is Command.update:
            mainlogger.info("Update mode. Running headless browser.")
            try:
                update_loop(browser_factory, tracker, args.timeout)
            except KeyboardInterrupt:
                pass
            finally:
                mainlogger.info("Shutting down...")
    finally:
        tracker.close()

if __name__ == "__main__":
    main()
