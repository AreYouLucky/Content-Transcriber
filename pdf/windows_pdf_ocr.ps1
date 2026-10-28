param(
    [Parameter(Mandatory=$true)][string]$InputPdf,
    [Parameter(Mandatory=$true)][string]$PageIndexes,
    [string]$Language = 'eng',
    [int]$Dpi = 300
)

# Windows' built-in PDF renderer and OCR avoid third-party native DLLs.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType=WindowsRuntime]

$asTaskOperation = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
$asTaskAction = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and -not $_.IsGenericMethod -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction'
} | Select-Object -First 1

function Wait-Operation($Operation, [Type]$ResultType) {
    $task = $asTaskOperation.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    $task.GetAwaiter().GetResult()
}

function Wait-Action($Operation) {
    $task = $asTaskAction.Invoke($null, @($Operation))
    $null = $task.GetAwaiter().GetResult()
}

# Map the common Tesseract language codes to Windows language tags.
$languageMap = @{ eng='en'; fil='fil'; spa='es'; fra='fr'; deu='de'; ita='it'; por='pt'; jpn='ja'; kor='ko'; chi_sim='zh-Hans'; chi_tra='zh-Hant' }
if ($Language.Contains('+')) {
    throw 'Windows OCR accepts one language at a time. Use --ocr-language eng or use the PyMuPDF OCR engine for multiple languages.'
}
$tag = if ($languageMap.ContainsKey($Language)) { $languageMap[$Language] } else { $Language }
$available = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages)
$selected = $available | Where-Object {
    $_.LanguageTag -eq $tag -or $_.LanguageTag.StartsWith($tag + '-', [StringComparison]::OrdinalIgnoreCase)
} | Select-Object -First 1
if ($null -eq $selected) {
    $tags = ($available | ForEach-Object { $_.LanguageTag }) -join ', '
    throw "Windows OCR language '$Language' is unavailable. Installed languages: $tags"
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($selected)
if ($null -eq $engine) { throw 'Windows could not create an OCR engine.' }

$file = Wait-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($InputPdf)) ([Windows.Storage.StorageFile])
$pdf = Wait-Operation ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
$results = @()
foreach ($indexString in $PageIndexes.Split(',')) {
    $index = [uint32]::Parse($indexString)
    if ($index -ge $pdf.PageCount) { throw "PDF page index out of range: $index" }
    $page = $pdf.GetPage($index)
    $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
    $bitmap = $null
    try {
        # Windows PDF page sizes are in 96-DPI device-independent pixels.
        $scale = $Dpi / 96.0
        $maxSide = [Math]::Max($page.Size.Width, $page.Size.Height) * $scale
        if ($maxSide -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension) {
            $scale *= [Windows.Media.Ocr.OcrEngine]::MaxImageDimension / $maxSide
        }
        $render = New-Object Windows.Data.Pdf.PdfPageRenderOptions
        $render.DestinationWidth = [uint32][Math]::Max(1, [Math]::Floor($page.Size.Width * $scale))
        $render.DestinationHeight = [uint32][Math]::Max(1, [Math]::Floor($page.Size.Height * $scale))
        Wait-Action ($page.RenderToStreamAsync($stream, $render))
        $stream.Seek(0)
        $decoder = Wait-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Wait-Operation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Wait-Operation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $text = ($result.Lines | ForEach-Object { $_.Text }) -join "`n"
        $results += [pscustomobject]@{ index=[int]$index; text=[string]$text }
    }
    finally {
        if ($null -ne $bitmap) { $bitmap.Dispose() }
        $stream.Dispose()
        $page.Dispose()
    }
}
ConvertTo-Json -InputObject $results -Depth 3 -Compress
