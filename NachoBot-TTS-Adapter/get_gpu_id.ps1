param([string]$TomlPath)
$m = Select-String -Path $TomlPath -Pattern '^\s*tts\s*=\s*"cuda:(\d+)"' | Select-Object -First 1
if ($m) { $m.Matches[0].Groups[1].Value } else { '0' }
