@echo off
chcp 65001 >nul
cd /d %~dp0

if not exist .venv\Scripts\python.exe (
    echo [初始化] 首次运行：创建虚拟环境并安装依赖...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --quiet
)

echo [启动] Amazon Listing 生成器运行在 http://127.0.0.1:8765
echo 关闭本窗口即可停止服务。
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8765"
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765
