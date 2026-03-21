' Launcher.vbs
' Usage: cscript Launcher.vbs <Username> <ServerIp> <JvmOpts>
' Stores all files in %LOCALAPPDATA%\PortableMC

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

' --- Download function with SSL fallback ---
Function DownloadFile(url, destPath)
    Dim allowInsecure
    allowInsecure = (UCase(Environment("ALLOW_INSECURE_SSL")) = "TRUE")
    On Error Resume Next
    Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    http.Open "GET", url, False
    http.Send
    If Err.Number = 0 And http.Status = 200 Then
        ' Success – write file
    Else
        If allowInsecure Then
            Err.Clear
            Set http = CreateObject("MSXML2.ServerXMLHTTP")
            http.Open "GET", url, False
            http.setOption 2, 13056   ' ignore certificate errors
            http.Send
            If Err.Number = 0 And http.Status = 200 Then
                ' Success with insecure fallback
            Else
                Exit Function
            End If
        Else
            Exit Function
        End If
    End If
    ' Write the response body to file
    Dim stream
    Set stream = CreateObject("ADODB.Stream")
    stream.Type = 1
    stream.Open
    stream.Write http.ResponseBody
    stream.SaveToFile destPath, 2
    stream.Close
    DownloadFile = True
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