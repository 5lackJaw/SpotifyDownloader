param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Playlist,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Downloader = Join-Path $ScriptDir "downloader.py"

python $Downloader $Playlist --skip-existing @ExtraArgs
exit $LASTEXITCODE
