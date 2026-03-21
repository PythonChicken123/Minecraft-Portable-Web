' Launcher.vbs
' Usage: cscript Launcher.vbs <Username> <ServerIp> <JvmOpts>
' Stores all files in %LOCALAPPDATA%\PortableMC and forces junction recreation for mods/resourcepacks.

Set args = WScript.Arguments
If args.Count < 3 Then
    WScript.Echo "Usage: cscript Launcher.vbs Username ServerIp JvmOpts"
    WScript.Quit 1
End If

username = args(0)
serverIp = args(1)
jvmOpts = args(2)

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
shell.Environment("PROCESS")("__COMPAT_LAYER") = "RUNASINVOKER"

' --- Get script directory ---
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
' Root directory is one level up
rootDir = fso.GetParentFolderName(scriptDir)

' --- Base directory in %LOCALAPPDATA% ---
localAppData = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
baseDir = fso.BuildPath(localAppData, "PortableMC")
If Not fso.FolderExists(baseDir) Then fso.CreateFolder(baseDir)

binDir = fso.BuildPath(baseDir, "portablemc_bin")
exePath = fso.BuildPath(binDir, "portablemc.exe")

' --- Helper to create a junction, removing any existing folder/link first ---
Sub CreateJunction(source, target)
    ' Ensure source folder exists (create if missing)
    If Not fso.FolderExists(source) Then fso.CreateFolder(source)
    ' If target already exists, delete it (whether folder or junction)
    If fso.FolderExists(target) Then
        fso.DeleteFolder target, True
        WScript.Echo "Removed existing target: " & target
    End If
    WScript.Echo "Creating junction: " & target & " -> " & source
    ' Use cmd /c mklink /J (target must not exist before)
    cmd = "cmd /c mklink /J """ & target & """ """ & source & """"
    shell.Run cmd, 0, True
    ' Junction creation doesn't set exit code easily; assume success if target now exists
    If fso.FolderExists(target) Then
        WScript.Echo "Junction created successfully."
    Else
        WScript.Echo "Failed to create junction. Ensure source exists and you're on the same drive."
    End If
End Sub

' --- Download function with SSL fallback ---
Function DownloadFile(url, destPath)
    On Error Resume Next
    Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    http.Open "GET", url, False
    http.SetTimeouts 10000, 10000, 10000, 10000
    http.Send
    If Err.Number = 0 And http.Status = 200 Then
        Dim binaryData
        binaryData = http.ResponseBody
        Set stream = CreateObject("ADODB.Stream")
        stream.Type = 1
        stream.Open
        stream.Write binaryData
        stream.SaveToFile destPath, 2
        stream.Close
        DownloadFile = True
        Exit Function
    End If
    ' Fallback to XMLHTTP with SSL ignore
    Set xmlHttp = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    xmlHttp.setOption 2, 13056
    xmlHttp.Open "GET", url, False
    xmlHttp.Send
    If Err.Number = 0 And xmlHttp.Status = 200 Then
        Set stream = CreateObject("ADODB.Stream")
        stream.Type = 1
        stream.Open
        stream.Write xmlHttp.ResponseBody
        stream.SaveToFile destPath, 2
        stream.Close
        DownloadFile = True
    Else
        WScript.Echo "Download failed: " & Err.Description
        DownloadFile = False
    End If
    On Error Goto 0
End Function

Sub ExtractZip(zipPath, destDir)
    If Not fso.FolderExists(destDir) Then fso.CreateFolder(destDir)
    Set zip = CreateObject("Shell.Application").NameSpace(zipPath)
    Set dest = CreateObject("Shell.Application").NameSpace(destDir)
    dest.CopyHere zip.Items, 284
    WScript.Sleep 5000
End Sub

' --- Main download and extraction ---
If Not fso.FileExists(exePath) Then
    WScript.Echo "portablemc.exe not found. Downloading..."
    url = "https://github.com/mindstorm38/portablemc/releases/download/v5.0.2/portablemc-5.0.2-windows-x86_64-msvc.zip"
    zipPath = fso.BuildPath(baseDir, "portablemc.zip")
    If Not DownloadFile(url, zipPath) Then
        WScript.Echo "Download failed. Exiting."
        WScript.Quit 1
    End If
    Set file = fso.GetFile(zipPath)
    WScript.Echo "Download complete. Size: " & file.Size & " bytes"
    WScript.Echo "Extracting to: " & binDir
    ExtractZip zipPath, binDir
    fso.DeleteFile zipPath, True

    ' Flatten subdirectories
    If fso.FolderExists(binDir) Then
        Set folder = fso.GetFolder(binDir)
        For Each subFolder In folder.SubFolders
            For Each file In subFolder.Files
                destFile = fso.BuildPath(binDir, file.Name)
                If fso.FileExists(destFile) Then fso.DeleteFile destFile, True
                file.Move destFile
            Next
            subFolder.Delete True
        Next
    End If
End If

If Not fso.FileExists(exePath) Then
    WScript.Echo "ERROR: portablemc.exe still not found at " & exePath
    WScript.Quit 1
End If

' --- Create junctions for mods and resourcepacks (force recreation) ---
CreateJunction fso.BuildPath(rootDir, "mods"), fso.BuildPath(baseDir, "mods")
CreateJunction fso.BuildPath(rootDir, "resourcepacks"), fso.BuildPath(baseDir, "resourcepacks")

' --- Build command line ---
jvmArgs = Replace(jvmOpts, " ", ",")
arguments = "--main-dir . start --join-server " & serverIp & " --jvm-arg=" & jvmArgs & " fabric: -u " & username

WScript.Echo "Launching: " & exePath & " " & arguments
WScript.Echo "Working directory: " & baseDir

' Set environment variable to run as invoker
shell.Environment("PROCESS")("__COMPAT_LAYER") = "RUNASINVOKER"

' Change to base directory and launch
shell.CurrentDirectory = baseDir
Set proc = shell.Exec("""" & exePath & """ " & arguments)

' Wait up to 30 seconds
Dim startTime, line
startTime = Timer
Do While Timer - startTime < 30
    While Not proc.StdOut.AtEndOfStream
        WScript.StdOut.WriteLine proc.StdOut.ReadLine
    Wend
    While Not proc.StdErr.AtEndOfStream
        WScript.StdErr.WriteLine proc.StdErr.ReadLine
    Wend
    If proc.Status <> 0 Then Exit Do
    WScript.Sleep 200
Loop

If proc.Status = 0 Then
    WScript.Echo "Process still running – detaching."
    WScript.Quit 0
Else
    While Not proc.StdOut.AtEndOfStream
        WScript.StdOut.WriteLine proc.StdOut.ReadLine
    Wend
    While Not proc.StdErr.AtEndOfStream
        WScript.StdErr.WriteLine proc.StdErr.ReadLine
    Wend
    WScript.Echo "Process exited with code " & proc.ExitCode
    WScript.Quit proc.ExitCode
End If