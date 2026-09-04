# Values that differ from one installation to the next. The live copy is
# C:\CouchGaming\config.psd1, beside the deployed scripts: Install.ps1 creates
# it from this file once, and Deploy.ps1 never overwrites it.
@{
    # The controller receiver as VirtualHere names it, and its hardware id as
    # Windows enumerates it. Its VirtualHere address is looked up per use.
    PuckName = 'Steam Controller Puck'
    PuckHwId = 'VID_28DE&PID_1304'

    # The TV's EDID name as Windows reports it. Doctor.ps1 lists what Windows
    # sees when this does not match.
    TvEdid   = 'QCQ90S'

    # Primary-display height that means the TV profile is active. No desk
    # monitor may share it.
    TvHeight = 2160
}
