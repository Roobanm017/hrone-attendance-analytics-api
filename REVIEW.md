# Employee Attendance & Analytics API

A REST API for managing employee records, attendance punch-in/punch-out, attendance regularization, and department-level analytics.

## Tech Stack

- Python 3.11+
- FastAPI
- MongoDB
- PyMongo
- Uvicorn

## Features

- Employee creation and listing
- Attendance punch-in and punch-out
- Attendance record retrieval and regularization
- Monthly employee attendance analytics
- Department attendance summary and trends
- Late-arrival leaderboard
- Query execution plan inspection for selected endpoints
- Health-check endpoint

## Prerequisites

- Python 3.11 or later
- MongoDB 6.0 or later
- Git

## Setup

Clone the repository:

```bash
git clone https://github.com/Roobanm017/hrone-attendance-analytics-api.git
cd hrone-attendance-analytics-api
```

Create and activate a virtual environment.

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Ensure MongoDB is running and configure the MongoDB connection using the configuration supported by `app/main.py`.

## Run the API

```bash
uvicorn app.main:app --port 8000
```

Open the interactive API documentation at:

http://127.0.0.1:8000/docs

Health check:

http://127.0.0.1:8000/health

## Main Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Health check |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees |
| POST | `/attendance/punch-in` | Record punch-in |
| POST | `/attendance/punch-out` | Record punch-out |
| GET | `/attendance` | Retrieve attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize attendance |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly employee analytics |
| GET | `/analytics/departments/summary` | Department summary |
| GET | `/analytics/leaderboard/late` | Late-arrival leaderboard |
| GET | `/analytics/departments/{department}/trend` | Department attendance trend |
| GET | `/admin/explain/{endpoint}` | Inspect query execution plans |

## Project Structure

```text
app/
  __init__.py
  main.py
requirements.txt
README.md
REVIEW.md
DECISIONS.md
```

## Supporting Documents

- `REVIEW.md` — defects identified in the starter implementation and their fixes.
- `DECISIONS.md` — implementation decisions and trade-offs.

## Notes

This project was developed as a technical assignment. Verify the configured database, API responses, and attendance calculations before using it in production.
