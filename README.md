# Python environment for JSSP

Python 3.13 virtual environment with Google OR-Tools installed for future
Job Shop Scheduling Problem scripts.

## Use the environment

Select `.venv\Scripts\python.exe` as your editor's Python interpreter, or run:

```powershell
.\.venv\Scripts\python.exe your_script.py
```

Optional activation in PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Recreate the environment

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Check dependencies

```powershell
.\.venv\Scripts\python.exe -m pip check
```
