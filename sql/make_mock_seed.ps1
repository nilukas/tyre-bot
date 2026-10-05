param([string]$OutSql, [string]$OutImages)

Add-Type -AssemblyName System.Drawing
New-Item -ItemType Directory -Force $OutImages | Out-Null

# id suffix, condition, depth mm, km, size, model, notes
$rows = @(
  @('01','new',      8.0,     0,'205/55 R16','Mock Touring A','Brand new, unused'),
  @('02','new',      7.8,  1500,'195/65 R15','Mock Touring B',''),
  @('03','new',      7.5,  3000,'225/45 R17','Mock Sport C',''),
  @('04','good',     6.5, 12000,'205/55 R16','Mock Touring A','Even wear'),
  @('05','good',     6.0, 18000,'195/65 R15','Mock Touring B','Even wear'),
  @('06','good',     5.5, 22000,'265/65 R17','Mock SUV D','SUV tyre, even wear'),
  @('07','good',     5.0, 26000,'225/45 R17','Mock Sport C',''),
  @('08','worn',     4.5, 30000,'205/55 R16','Mock Touring A',''),
  @('09','worn',     4.0, 34000,'205/55 R16','Mock Touring A','Inner-edge wear 2.8 mm; alignment issue'),
  @('10','worn',     3.5, 38000,'195/65 R15','Mock Touring B',''),
  @('11','worn',     3.0, 42000,'225/45 R17','Mock Sport C','Centre wear; over-inflation'),
  @('12','at_limit', 2.0, 48000,'205/55 R16','Mock Touring A','Close to wear bars'),
  @('13','at_limit', 1.6, 52000,'195/65 R15','Mock Touring B','Wear bars flush with tread'),
  @('14','at_limit', 1.2, 56000,'265/65 R17','Mock SUV D','Below legal limit'),
  @('15','damaged',  5.5, 20000,'205/55 R16','Mock Touring A','Sidewall bulge (impact damage) - unsafe'),
  @('16','damaged',  4.0, 30000,'195/65 R15','Mock Touring B','Sidewall cracking / dry rot, 9 years old'),
  @('17','damaged',  6.0, 15000,'225/45 R17','Mock Sport C','Nail in tread centre - puncture')
)

$colors = @{
  new      = [System.Drawing.Color]::FromArgb(63,155,99)
  good     = [System.Drawing.Color]::FromArgb(63,155,99)
  worn     = [System.Drawing.Color]::FromArgb(201,162,39)
  at_limit = [System.Drawing.Color]::FromArgb(200,69,59)
  damaged  = [System.Drawing.Color]::FromArgb(200,69,59)
}

$jpegCodec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
$encParams = New-Object System.Drawing.Imaging.EncoderParameters 1
$encParams.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality), 75L

function Sql([string]$s) { if ($s -eq '') { 'NULL' } else { "N'" + $s.Replace("'", "''") + "'" } }

$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine(@"
/* ===========================================================================
   Tyre Advisor - MOCK reference tyres for local testing
   ---------------------------------------------------------------------------
   * Start the app once with DATA_BACKEND=sqlserver first: it creates the
     database and tables. Then run this script (SSMS, Azure Data Studio/VS Code
     mssql extension, or sqlcmd).
   * Images are placeholder DRAWINGS labelled "MOCK", not real photos. They are
     fine with AI_BACKEND=mock; replace them with real gauge-measured photos
     (section 2) before testing with AI_BACKEND=foundry.
   * Safe to re-run: rows are only inserted if their id does not exist.
   * Remove everything again with section 3.
   =========================================================================== */
USE [TyreAdvisor];
SET NOCOUNT ON;

-- 1. Insert mock reference tyres ---------------------------------------------
"@)

foreach ($r in $rows) {
  $id = 'a11ce000000000000000000000000' + '0' + $r[0]
  $cond = $r[1]; $depth = [double]$r[2]; $km = [int]$r[3]

  # ---- draw placeholder tread illustration ----
  $bmp = New-Object System.Drawing.Bitmap 480, 320
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode = 'AntiAlias'
  $g.Clear([System.Drawing.Color]::FromArgb(235,236,238))
  $base = 250; $scale = 18
  $g.FillRectangle((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(70,70,75))), 30, $base, 420, 30)
  $brush = New-Object System.Drawing.SolidBrush $colors[$cond]
  $h = [int]($depth * $scale)
  for ($i = 0; $i -lt 5; $i++) {
    $x = 30 + $i * 90
    $g.FillRectangle($brush, $x, $base - $h, 60, $h)
    if ($i -lt 4) {
      $g.FillRectangle((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(120,120,125))), $x + 60, $base - [int](1.6 * $scale), 30, [int](1.6 * $scale))
    }
  }
  if ($cond -eq 'damaged') {
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(200,30,30)), 5
    $g.DrawEllipse($pen, 300, 225, 120, 70)
  }
  $font = New-Object System.Drawing.Font 'Segoe UI', 18, ([System.Drawing.FontStyle]::Bold)
  $small = New-Object System.Drawing.Font 'Segoe UI', 12
  $ink = [System.Drawing.Brushes]::Black
  $g.DrawString("MOCK REFERENCE - $cond", $font, $ink, 24, 14)
  $g.DrawString(("{0:0.0} mm tread  |  {1:N0} km  |  {2}" -f $depth, $km, $r[4]), $small, $ink, 26, 50)
  if ($r[6]) { $g.DrawString($r[6], $small, $ink, 26, 74) }
  $g.Dispose()

  $path = Join-Path $OutImages "$id.jpg"
  $bmp.Save($path, $jpegCodec, $encParams)
  $bmp.Dispose()
  $bytes = [System.IO.File]::ReadAllBytes($path)
  $hex = '0x' + [System.BitConverter]::ToString($bytes).Replace('-', '')

  [void]$sb.AppendLine("IF NOT EXISTS (SELECT 1 FROM dbo.reference_tyres WHERE id = '$id')")
  [void]$sb.AppendLine("INSERT INTO dbo.reference_tyres (id, condition, tread_depth_mm, km_driven, tyre_size, brand_model, notes, created_by, created_at, image)")
  [void]$sb.AppendLine(("VALUES ('{0}', '{1}', {2}, {3}, {4}, {5}, {6}, N'mock-seed', SYSUTCDATETIME(),`n        {7});" -f $id, $cond, $depth.ToString([System.Globalization.CultureInfo]::InvariantCulture), $km, (Sql $r[4]), (Sql $r[5]), (Sql $r[6]), $hex))
  [void]$sb.AppendLine("")
}

[void]$sb.AppendLine(@"
SELECT condition, COUNT(*) AS photos, MIN(tread_depth_mm) AS min_mm, MAX(tread_depth_mm) AS max_mm
FROM dbo.reference_tyres WHERE created_by = N'mock-seed'
GROUP BY condition ORDER BY MAX(tread_depth_mm) DESC;

/* 2. OPTIONAL: swap a placeholder for a real photo ------------------------------
   Copy your JPEG to a folder the SQL Server service can read, then run (one per photo).
   Needs bulk-load permission (sysadmin has it on a local install).
   Prefer uploading real photos through /admin instead: the app validates them and
   strips EXIF/GPS metadata, which this direct load does NOT do.

UPDATE dbo.reference_tyres
SET image = (SELECT BulkColumn FROM OPENROWSET(BULK N'C:\TyrePhotos\new_8mm.jpg', SINGLE_BLOB) AS f),
    tread_depth_mm = 8.0
WHERE id = '__FIRST_ID__';
*/

/* 3. Remove all mock rows ---------------------------------------------------------
DELETE FROM dbo.reference_tyres WHERE created_by = N'mock-seed';
*/
"@)

$firstId = 'a11ce000000000000000000000000' + '0' + $rows[0][0]
[System.IO.File]::WriteAllText($OutSql, $sb.ToString().Replace('__FIRST_ID__', $firstId), (New-Object System.Text.UTF8Encoding $false))
"Wrote $($rows.Count) rows; SQL size {0:N0} KB" -f ((Get-Item $OutSql).Length / 1KB)
