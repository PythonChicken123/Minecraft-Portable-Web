using System;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Diagnostics;

class Program
{
    static void Main(string[] args)
    {
        // Force TLS 1.2
        ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12 | SecurityProtocolType.Tls11 | SecurityProtocolType.Tls;

        if (args.Length < 3)
        {
            Console.WriteLine("Usage: PortableMCLoader.exe Username ServerIp JvmOpts");
            Environment.Exit(1);
        }

        string username = args[0];
        string serverIp = args[1];
        string jvmOpts = args[2];

        string baseDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PortableMC");
        Directory.CreateDirectory(baseDir);
        string binDir = Path.Combine(baseDir, "portablemc_bin");
        string exePath = Path.Combine(binDir, "portablemc.exe");

        if (!File.Exists(exePath))
        {
            Console.WriteLine("portablemc.exe not found. Downloading...");
            string url = "https://github.com/mindstorm38/portablemc/releases/download/v5.0.2/portablemc-5.0.2-windows-x86_64-msvc.zip";
            string zipPath = Path.Combine(baseDir, "portablemc.zip");

            if (!DownloadFile(url, zipPath))
            {
                Environment.Exit(1);
            }

            Console.WriteLine("Extracting...");
            try
            {
                ZipFile.ExtractToDirectory(zipPath, binDir);
            }
            catch (Exception ex)
            {
                Console.WriteLine(string.Format("Extraction failed: {0}", ex.Message));
                Environment.Exit(1);
            }
            finally
            {
                File.Delete(zipPath);
            }

            FlattenDirectory(binDir);
            Console.WriteLine("Extraction complete.");
        }

        string jvmArgs = jvmOpts.Replace(' ', ',');
        string arguments = string.Format("--main-dir . start --join-server {0} --jvm-arg={1} fabric: -u {2}",
            serverIp, jvmArgs, username);

        Console.WriteLine(string.Format("Launching: {0} {1}", exePath, arguments));
        Console.WriteLine(string.Format("Working directory: {0}", baseDir));

        Environment.SetEnvironmentVariable("__COMPAT_LAYER", "RUNASINVOKER");

        ProcessStartInfo psi = new ProcessStartInfo
        {
            FileName = exePath,
            Arguments = arguments,
            WorkingDirectory = baseDir,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true
        };

        using (Process proc = Process.Start(psi))
        {
            proc.OutputDataReceived += (s, e) => { if (e.Data != null) Console.WriteLine(e.Data); };
            proc.ErrorDataReceived += (s, e) => { if (e.Data != null) Console.Error.WriteLine(e.Data); };
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();

            if (proc.WaitForExit(30000))
            {
                Console.WriteLine(string.Format("Process exited with code {0}", proc.ExitCode));
                Environment.Exit(proc.ExitCode);
            }
            else
            {
                Console.WriteLine("Process still running – detaching.");
                Environment.Exit(0);
            }
        }
    }

    static bool DownloadFile(string url, string destPath)
    {
        // Manual check for environment variable (C# 5 compatible)
        string envVar = Environment.GetEnvironmentVariable("ALLOW_INSECURE_SSL");
        bool allowInsecure = envVar != null && (envVar.ToLower() == "true" || envVar == "1" || envVar.ToLower() == "yes");

        try
        {
            Console.WriteLine(string.Format("Downloading {0} -> {1}", url, destPath));
            using (WebClient client = new WebClient())
            {
                client.DownloadFile(url, destPath);
            }
            return true;
        }
        catch (Exception ex)
        {
            Console.WriteLine(string.Format("First download attempt failed: {0}", ex.Message));
            if (allowInsecure)
            {
                Console.WriteLine("Retrying with SSL verification disabled...");
                try
                {
                    ServicePointManager.ServerCertificateValidationCallback = (sender, cert, chain, errors) => true;
                    using (WebClient client = new WebClient())
                    {
                        client.DownloadFile(url, destPath);
                    }
                    return true;
                }
                catch (Exception ex2)
                {
                    Console.WriteLine(string.Format("Download failed even with SSL disabled: {0}", ex2.Message));
                    return false;
                }
            }
            else
            {
                Console.WriteLine("SSL verification failed and insecure SSL is disabled.");
                return false;
            }
        }
    }

    static void FlattenDirectory(string dir)
    {
        if (!Directory.Exists(dir)) return;
        foreach (string subDir in Directory.GetDirectories(dir))
        {
            foreach (string file in Directory.GetFiles(subDir))
            {
                string dest = Path.Combine(dir, Path.GetFileName(file));
                if (File.Exists(dest)) File.Delete(dest);
                File.Move(file, dest);
            }
            Directory.Delete(subDir, true);
        }
    }
}
