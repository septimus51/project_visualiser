# Code Dependency Analyzer

A containerized Python tool that analyzes code projects to build a dependency graph — mapping which functions call which functions across files, and which files import which files. Outputs a JSON graph and an error report for unprocessed files.

---

## Features

- **Multi-language support**: Python (full AST), JavaScript/TypeScript, Java, C/C++, Go, Ruby, PHP, Rust, C#, Kotlin, Swift, Shell, and more
- **Two analysis modes**:
  - **Python**: Native AST parsing (most accurate)
  - **Other languages**: Regex-based fallback analysis
- **Comprehensive graph output**: Files, functions, classes, imports, and call relationships
- **Error tracking**: Unprocessed files logged separately with reasons
- **Interactive visualization**: D3.js-based HTML viewer
- **Static visualization**: PNG generation via NetworkX + Matplotlib
- **Dockerized**: Runs anywhere without local Python dependencies

---

## Quick Start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (or Docker Desktop on Windows)
- [Docker Compose](https://docs.docker.com/compose/install/) (optional)

---

## Installation

### 1. Clone or download the project

```bash
git clone &lt;repository-url&gt;
cd code-analyzer
```
### 2. Build the Docker image

```bash
docker build -t code-analyzer .
```
### 3. Usage

```bash
docker run -v /path/to/your/project:/project:ro \
           -v $(pwd)/output:/output \
           code-analyzer /project \
           -o /output/dependency_graph.json \
           -e /output/failed_files.txt
```

Arguments:
/project — Path inside the container to your source code (mounted read-only)

-o — Output JSON graph path

-e — Error report path

Example: Analyze a Python project

```bash
# Linux / macOS
docker run -v $(pwd)/my-python-app:/project:ro \
           -v $(pwd)/output:/output \
           code-analyzer /project \
           -o /output/dependency_graph.json \
           -e /output/failed_files.txt

# Windows (PowerShell)
docker run -v ${PWD}/my-python-app:/project:ro `
           -v ${PWD}/output:/output `
           code-analyzer /project `
           -o /output/dependency_graph.json `
           -e /output/failed_files.txt

# Windows (Command Prompt)
docker run -v %cd%/my-python-app:/project:ro ^
           -v %cd%/output:/output ^
           code-analyzer /project ^
           -o /output/dependency_graph.json ^
           -e /output/failed_files.txt
```
### 4. Visualization

```bash
python3 -m http.server 8000
```
Open http://localhost:8000/visualize.html