param([Parameter(Mandatory=$true)][string]$RunDirectory, [int]$AltiumProcessId = 26484)
$ErrorActionPreference = 'Stop'
$base = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\docs\validation'))
$folder = [IO.Path]::GetFullPath($RunDirectory)
if(-not $folder.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $folder -PathType Container)){throw 'Unexpected evidence directory'}
foreach($name in @('batch-before.json','batch-effective.json','report-options-effective.json')){
    if(Test-Path -LiteralPath (Join-Path $folder $name)){throw 'Existing UI evidence must not be overwritten'}
}
if((Get-Process -Id $AltiumProcessId).ProcessName -ne 'X2'){throw 'Not Altium'}
Add-Type -AssemblyName UIAutomationClient
Add-Type -TypeDefinition 'using System.Runtime.InteropServices; public static class DrcSupervisedMouse { [DllImport("user32.dll")] public static extern bool SetCursorPos(int x,int y); [DllImport("user32.dll")] public static extern void mouse_event(uint f,uint x,uint y,uint d,System.UIntPtr e); }'
function ClickAt([int]$x,[int]$y){
    [void][DrcSupervisedMouse]::SetCursorPos($x,$y)
    [DrcSupervisedMouse]::mouse_event(2,0,0,0,[System.UIntPtr]::Zero)
    [DrcSupervisedMouse]::mouse_event(4,0,0,0,[System.UIntPtr]::Zero)
    Start-Sleep -Milliseconds 250
}
function Named($parent,[string]$name){
    $parent.FindFirst([System.Windows.Automation.TreeScope]::Descendants,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty,$name)))
}
function BatchRows($dialog){
    $list = Named $dialog 'RulesOnOffList'
    @($list.FindAll([System.Windows.Automation.TreeScope]::Children,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::ListItem))) | ForEach-Object {
        [pscustomobject]@{name=$_.Current.Name;value=$_.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value}
    })
}
$root=[System.Windows.Automation.AutomationElement]::RootElement
$dialog=$root.FindFirst([System.Windows.Automation.TreeScope]::Children,(New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ClassNameProperty,'TDesignRuleCheckForm')))
if(-not $dialog -or $dialog.Current.ProcessId -ne $AltiumProcessId -or $dialog.Current.BoundingRectangle.ToString() -ne '478,72,963,875'){throw 'Supervised layout no longer matches observed AD26 dialog'}
$rect=(Named $dialog 'Rule Types to Check').Current.BoundingRectangle
ClickAt ([int]($rect.X+80)) ([int]($rect.Y+10))
$before=BatchRows $dialog
$before | ConvertTo-Json | Out-File -LiteralPath (Join-Path $folder 'batch-before.json') -Encoding utf8
# Coordinates correspond to the observed AD26 menu in this fixed dialog layout.
[void][DrcSupervisedMouse]::SetCursorPos(1353,262)
[DrcSupervisedMouse]::mouse_event(8,0,0,0,[System.UIntPtr]::Zero)
[DrcSupervisedMouse]::mouse_event(16,0,0,0,[System.UIntPtr]::Zero)
Start-Sleep -Milliseconds 250
ClickAt 1450 400
$rows=BatchRows $dialog
if($rows.Count -ne 55 -or @($rows | Where-Object {$_.value -notmatch 'True\s*$'}).Count){throw 'Batch readback mismatch'}
$rows | ConvertTo-Json | Out-File -LiteralPath (Join-Path $folder 'batch-effective.json') -Encoding utf8
$rect=(Named $dialog 'Report Options').Current.BoundingRectangle
ClickAt ([int]($rect.X+80)) ([int]($rect.Y+10))
$edit=Named $dialog '30'
if($edit){$edit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue('100000')}
$toggle=(Named $dialog 'Report PCB Health Issues').GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
if($toggle.Current.ToggleState -eq [System.Windows.Automation.ToggleState]::Off){$toggle.Toggle()}
$options=@($dialog.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition) | ForEach-Object {
    if($_.Current.ControlType -eq [System.Windows.Automation.ControlType]::CheckBox -or $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Edit){
        [pscustomobject]@{name=$_.Current.Name;value=$_.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value}
    }
})
if(@($options | Where-Object {$_.name -eq '100000' -and $_.value -eq '100000'}).Count -ne 1){throw 'Threshold mismatch'}
$options | ConvertTo-Json | Out-File -LiteralPath (Join-Path $folder 'report-options-effective.json') -Encoding utf8
(Named $dialog 'OK').GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
'Confirmed 55 Batch types and threshold 100000. This is not analysis acceptance.'
