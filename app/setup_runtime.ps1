# 「初めて使う方はこちら.bat」から呼ばれる。Python が入っていない PC でも動くように、
# このフォルダの runtime\ に Python（embeddable 版）と部品（requirements.txt）を用意する。
# インターネット接続が必要（python.org・bootstrap.pypa.io・pypi.org）。
param([string]$Root = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = "Stop"

# Python を呼ぶ時は、Python が標準エラーに出す警告で止まらないようにする（Windows PowerShell 5.1 対策）。
# 成否は終了コードで判定する。
function Invoke-Py {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Python @args 2>&1 | ForEach-Object { "$_" } | Write-Host } finally { $ErrorActionPreference = $prev }
    return $LASTEXITCODE
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = "SilentlyContinue"

$PyVersion = "3.13.7"
$Runtime = Join-Path $Root "runtime"
$Python = Join-Path $Runtime "python.exe"
$Temp = Join-Path ([IO.Path]::GetTempPath()) ("meishi_setup_" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Temp | Out-Null

try {
    if (-not (Test-Path $Python)) {
        Write-Host "[1/3] Python $PyVersion をダウンロードしています..."
        $zip = Join-Path $Temp "python-embed.zip"
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip" -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath $Runtime -Force
        # runtime\Lib\site-packages と、このフォルダ（app パッケージ）を import できるようにする
        $pth = Get-ChildItem -Path $Runtime -Filter "python*._pth" | Select-Object -First 1
        Set-Content -Path $pth.FullName -Encoding ASCII -Value @("python313.zip", ".", "Lib\site-packages", "..")
    } else {
        Write-Host "[1/3] Python は用意済みです（runtime\python.exe）"
    }

    Write-Host "[2/3] 部品のインストーラー（pip）を用意しています..."
    $site = Join-Path $Runtime "Lib\site-packages"
    New-Item -ItemType Directory -Path $site -Force | Out-Null
    $hasPip = Invoke-Py -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('pip') else 1)"
    if ($hasPip -ne 0) {
        $getpip = Join-Path $Temp "get-pip.py"
        Invoke-WebRequest -UseBasicParsing -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getpip
        if ((Invoke-Py $getpip --no-warn-script-location --disable-pip-version-check --target $site) -ne 0) {
            throw "pip の用意に失敗しました"
        }
    }

    Write-Host "[3/3] 部品（PyMuPDF・openpyxl）をインストールしています..."
    $req = Join-Path $Root "requirements.txt"
    if ((Invoke-Py -m pip install --disable-pip-version-check --no-warn-script-location --upgrade --target $site -r $req) -ne 0) {
        throw "部品のインストールに失敗しました"
    }

    if ((Invoke-Py -X utf8 -c "import fitz, openpyxl; print('準備完了: PyMuPDF', fitz.__doc__.split()[1].rstrip(':'), '/ openpyxl', openpyxl.__version__)") -ne 0) {
        throw "部品の読み込みに失敗しました"
    }
    exit 0
}
catch {
    Write-Host ""
    Write-Host "準備に失敗しました: $($_.Exception.Message)"
    Write-Host "インターネットにつながっているか（会社のプロキシ設定を含む）を確認して、もう一度実行してください。"
    exit 1
}
finally {
    Remove-Item -Recurse -Force $Temp -ErrorAction SilentlyContinue
}
