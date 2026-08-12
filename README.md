# Schema Field Mapper

AI pipeline that maps fields from a legacy MySQL HR schema (`legacy_hrm`) to a MongoDB people platform schema (`people_platform`), producing a single mapping JSON document.

Assignment details: `InterviewAssignment.txt`.

## Setup

Requires **Python 3.12+**. Dependencies in `requirements.txt` are cross-platform (Windows and Linux/WSL).

**Important:** a `.venv` created on Windows cannot be used from WSL (and vice versa). Use the setup script on whichever OS you are running Python on; it removes the other platform's venv automatically.

### Windows (PowerShell)

```powershell
.\scripts\setup_venv.ps1
.\.venv\Scripts\Activate.ps1
```

Manual equivalent:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
```

### WSL / Linux / macOS

WSL Ubuntu 20.04 ships Python 3.8 by default — install 3.12 once (see script output if missing):

```bash
sudo apt update
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev
```

Then:

```bash
bash scripts/setup_venv.sh
source .venv/bin/activate
```

Copy or edit `.env` for this project only (never commit it). Use LLM/provider keys you actually need here.

## Run

Pipeline entrypoint and commands will be documented here once the code exists.

## Deliverables

- Working pipeline code
- Generated mapping JSON for the provided schemas
- Short write-up on prompt structure and design decisions

## Agent notes

- Coding agents: see `AGENTS.md`
- Durable session/decision log: `MEMORY.md`
