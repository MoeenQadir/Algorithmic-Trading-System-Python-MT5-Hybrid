$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$logs = Join-Path $root "logs"
$frontendPort = 3000

if (-not (Test-Path $python)) {
	throw "Python environment not found at $python. Create it with: py -3.12 -m venv .venv"
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
	throw "Node.js and npm are required to start the dashboard."
}
while ($true) {
	$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $frontendPort)
	try {
		$listener.Start()
		$listener.Stop()
		break
	} catch {
		$frontendPort++
	}
}
if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
	Write-Host "Installing dashboard dependencies..." -ForegroundColor Cyan
	Push-Location $frontend
	try { npm install } finally { Pop-Location }
	if ($LASTEXITCODE -ne 0) { throw "npm install failed." }
}

New-Item -ItemType Directory -Force -Path $logs | Out-Null
$env:PYTHONPATH = $backend
$null = Start-Process -FilePath $python `
	-ArgumentList @("-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000") `
	-WorkingDirectory $root -PassThru `
	-RedirectStandardOutput (Join-Path $logs "backend-stdout.log") `
	-RedirectStandardError (Join-Path $logs "backend-stderr.log")
$null = Start-Process -FilePath "powershell.exe" `
	-ArgumentList @("-NoProfile", "-Command", "Set-Location '$frontend'; npm run dev -- --port $frontendPort") `
	-WorkingDirectory $frontend

Write-Host "Backend:   http://127.0.0.1:8000/docs" -ForegroundColor Green
Write-Host "Dashboard: http://127.0.0.1:$frontendPort/dashboard" -ForegroundColor Green
Start-Process "http://127.0.0.1:$frontendPort/dashboard"