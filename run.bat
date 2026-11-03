@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [bfs] 正在创建虚拟环境 .venv ...
  python -m venv .venv
  if errorlevel 1 goto :fail
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -e ".[dev]"
  if errorlevel 1 goto :fail
)

if "%BFS_DATA_DIR%"=="" set BFS_DATA_DIR=%~dp0data
if "%BFS_HOST%"=="" set BFS_HOST=127.0.0.1

echo [bfs] http://%BFS_HOST%:8100
".venv\Scripts\python.exe" -m bfs serve --host %BFS_HOST% --port 8100
goto :eof

:fail
echo [bfs] 初始化失败，请检查 Python 是否在 PATH 中
exit /b 1
