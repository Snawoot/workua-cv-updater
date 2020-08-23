#!/usr/bin/env python3

import logging
import argparse
import enum
import os
import os.path
import sqlite3
import signal
from time import sleep, time, ctime
from random import randrange, random
from urllib.parse import urlparse, urlunparse, urlencode

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (TimeoutException,
                                        StaleElementReferenceException,
                                        NoSuchElementException,
                                        ElementClickInterceptedException)
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.utils import ChromeType

RESUME_LIST_URL = "https://www.work.ua/ru/jobseeker/my/resumes/"
LOGIN_URL = "https://www.work.ua/jobseeker/login/"
POST_LOGIN_URL = "https://www.work.ua/ru/jobseeker/my/"
UPDATE_BUTTON_XPATH = "//a[contains(@href, '/update/expire_date')]"
UPDATE_INTERVAL = 7 * 24 * 3600
UPDATE_INTERVAL_MIN_DRIFT = 10
UPDATE_INTERVAL_MAX_DRIFT = 60

DB_INIT = [
    "CREATE TABLE IF NOT EXISTS update_ts (\n"
    "name TEXT PRIMARY KEY,\n"
    "value REAL NOT NULL DEFAULT 0)\n"
]

def wall_clock_sleep(duration, precision=1.):
    """ Sleep variation which is doesn't increases
    sleep duration when computer enters suspend/hybernation
    """
    end_time = time() + duration
    while time() < end_time:
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
            logger.debug("Clicked!")
            WebDriverWait(browser, timeout).until(
                EC.staleness_of(elem)
            )
            WebDriverWait(browser, timeout).until(
                EC.url_to_be(RESUME_LIST_URL)
            )
            break
        else:
            break
    logger.info('Updated!')

def login(browser):
    logger = logging.getLogger("LOGIN")
    browser.get(LOGIN_URL)
    WebDriverWait(browser, 3600).until(
        EC.url_to_be(POST_LOGIN_URL)
    )
    logger.info('Successfully logged in!')

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
    def __init__(self, profile_dir, browser_type, headless=True):
        chrome_options = Options()
        # option below causes webdriver process remaining in memory
        # chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('user-data-dir=' + profile_dir)
        if headless:
            chrome_options.add_argument('--headless')
        self._options = chrome_options
        self._driver = ChromeDriverManager(chrome_type=browser_type).install()

    def new(self):
        return webdriver.Chrome(
            self._driver,
            options=self._options)

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

    def update(self, ts):
        c = self._conn
        with c:
            c.execute("UPDATE update_ts SET value = ? WHERE name = ? AND value < ?",
                      (float(ts), "last", float(ts)))

    def close(self):
        self._conn.close()
        self._conn = None

def do_login(browser_factory):
    browser = browser_factory.new()
    try:
        login(browser)
    finally:
        browser.quit()

def do_update(browser_factory, timeout):
    browser = browser_factory.new()
    try:
        update(browser, timeout)
    finally:
        browser.quit()

def random_interval():
    return UPDATE_INTERVAL + UPDATE_INTERVAL_MIN_DRIFT + \
        random() * (UPDATE_INTERVAL_MAX_DRIFT - UPDATE_INTERVAL_MIN_DRIFT)

def update_loop(browser_factory, tracker, timeout):
    logger = logging.getLogger("UPDATE")
    last_ts = tracker.last_update()
    logger.info("Starting scheduler. Last update @ %.3f (%s)",
                last_ts, ctime(last_ts))
    delay = last_ts + random_interval() - time()
    if delay > 0:
        logger.info("Waiting %.3f seconds for next update...", delay)
        wall_clock_sleep(delay)
    while True:
        try:
            logger.info("Updating now!")
            do_update(browser_factory, timeout)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logger.exception("Update failed: %s", str(exc))
        else:
            tracker.update(time())
        delay = random_interval()
        logger.info("Waiting %.3f seconds for next update...", delay)
        wall_clock_sleep(delay)

def sig_handler(signum, frame):
    raise KeyboardInterrupt

def main():
    args = parse_args()
    mainlogger = setup_logger("MAIN", args.verbosity)
    setup_logger("UPDATE", args.verbosity)
    setup_logger("LOGIN", args.verbosity)

    os.makedirs(args.data_dir, mode=0o700, exist_ok=True)
    profile_dir = os.path.join(args.data_dir, 'profile')
    browser_factory = BrowserFactory(profile_dir,
                                     args.browser.value,
                                     args.cmd is Command.update)

    if args.cmd is Command.login:
        mainlogger.info("Login mode. Please enter your credentials in opened "
                        "browser window.")
        do_login(browser_factory)
    elif args.cmd is Command.update:
        mainlogger.info("Update mode. Running headless browser.")
        signal.signal(signal.SIGTERM, sig_handler)
        db_path = os.path.join(args.data_dir, 'updater.db')
        tracker = UpdateTracker(db_path)
        try:
            update_loop(browser_factory, tracker, args.timeout)
        except KeyboardInterrupt:
            pass
        finally:
            mainlogger.info("Shutting down...")
            tracker.close()

if __name__ == "__main__":
    main()
