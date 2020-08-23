workua-cv-updater
=============

Python script to update your CV on https://work.ua/

## Requirements 

You need to have installed Chromium or Google Chrome browser.

## Installation

Run within source directory:

`pip3 install .`

`workua-cv-updater` executable command should become immediately available. Alternatively, you may invoke application with `python3 -m workua_cv_updater ...` command

## Set your account

Run:

```
workua-cv-updater login
```

Browser window will pop up, prompting user to login. Once login will be acknowledged by application, browser window will be closed.

## Update all resumes

Run:

```
workua-cv-updater update
```

Application will be running continously, updating all your CV's in random intervals about to 4 hours. If application is being restarted, it will figure out next update from own records on last update.

## Datadir structure

```
~/.config/workua-cv-updater
├── updater.db # SQLite database with last update timestamp
└── profile    # browser profile
```

## Running on remote server

* Option 1: use X forwading via SSH in order to perform login via browser UI: `ssh -Y user@host`
* Option 2: perform login on your local system and copy datadir to remote server

## Synopsis

```
$ workua-cv-updater -h
usage: workua-cv-updater [-h] [-t TIMEOUT] [-b {chrome,chromium}]
                     [-v {debug,info,warn,error,fatal}] [-d FILE]
                     {login,update}

Python script to update your CV

positional arguments:
  {login,update}        command

optional arguments:
  -h, --help            show this help message and exit
  -t TIMEOUT, --timeout TIMEOUT
                        webdriver wait timeout (default: 10.0)
  -b {chrome,chromium}, --browser {chrome,chromium}
                        browser type (default: chromium)
  -v {debug,info,warn,error,fatal}, --verbosity {debug,info,warn,error,fatal}
                        logging verbosity (default: info)
  -d FILE, --data-dir FILE
                        application datadir location (default:
                        ~/.config/workua-cv-updater)
```
