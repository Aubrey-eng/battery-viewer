# 输出当前「已连接」的 BLE 设备地址列表（JSON 数组，12 位大写十六进制）
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($operation, [Type]$resultType) {
    $task = $asTaskGeneric.MakeGenericMethod($resultType).Invoke($null, @($operation))
    $task.Wait(-1) | Out-Null
    $task.Result
}

$null = [Windows.Devices.Bluetooth.BluetoothLEDevice, Windows.Devices.Bluetooth, ContentType = WindowsRuntime]
$null = [Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType = WindowsRuntime]

$selector = [Windows.Devices.Bluetooth.BluetoothLEDevice]::GetDeviceSelectorFromConnectionStatus(
    [Windows.Devices.Bluetooth.BluetoothConnectionStatus]::Connected)
$infos = Await ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($selector)) ([Windows.Devices.Enumeration.DeviceInformationCollection])

$addresses = @()
foreach ($info in $infos) {
    # Id 形如 BluetoothLE#BluetoothLE<本地地址>-<设备地址>
    $parts = $info.Id -split '-'
    $tail = $parts[$parts.Count - 1].Replace(':', '').ToUpper()
    if ($tail.Length -ge 12) { $addresses += $tail.Substring($tail.Length - 12) }
}
# 注意：空数组走管道会什么都不输出，必须用 -InputObject 才能得到 []
ConvertTo-Json -InputObject @($addresses) -Compress
