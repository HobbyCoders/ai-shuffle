param([ValidateSet('read','write')][string]$Action,[string]$Path)
$ErrorActionPreference='Stop'
if($Action -eq 'write') {
    $text=[Console]::In.ReadToEnd()
    if($text -notmatch '^[a-f0-9]{64}$'){throw 'Invalid update credential'}
    $encrypted=ConvertFrom-SecureString (ConvertTo-SecureString $text -AsPlainText -Force)
    [IO.File]::WriteAllText($Path + '.tmp',$encrypted)
    Move-Item -LiteralPath ($Path + '.tmp') -Destination $Path -Force
} else {
    $secure=ConvertTo-SecureString ([IO.File]::ReadAllText($Path))
    $pointer=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try{[Console]::Out.Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer))}
    finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)}
}
