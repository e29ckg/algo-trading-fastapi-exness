@echo off
title 🤖 Algo Trading Bot Server
color 0A

echo ==================================================
echo       🚀 Starting Trading Bot Server...
echo ==================================================
echo.

REM ตรวจสอบและเปิดใช้งาน Virtual Environment (env) อัตโนมัติ
if exist env\Scripts\activate.bat (
    echo [System] Activating Virtual Environment...
    call env\Scripts\activate.bat
) else (
    echo [Warning] Virtual Environment 'env' not found. Running system Python...
)

echo.
echo [System] Running server.py...
echo ==================================================

REM สั่งรันไฟล์เซิร์ฟเวอร์
python server.py

REM ป้องกันไม่ให้หน้าต่างปิดเองถ้าเกิด Error
pause