using System;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Diagnostics;

class Program
{
    static void Main(string[] args)
    {
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
            try
            {
                using (WebClient client = new WebClient())
                {
                    client.DownloadFile(url, zipPath);
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine("Download failed: " + ex.Message);
                Console.WriteLine("Retrying with SSL disabled...");
                try
                {
                    ServicePointManager.ServerCertificateValidationCallback = (sender, cert, chain, errors) => true;
                    using (WebClient client = new WebClient())
                    {
                        client.DownloadFile(url, zipPath);
                    }
                }
                catch (Exception ex2)
                {
                    Console.WriteLine("Download failed even with SSL disabled: " + ex2.Message);
                    Environment.Exit(1);
                }
            }

            Console.WriteLine("Extracting...");
            ZipFile.ExtractToDirectory(zipPath, binDir);
            File.Delete(zipPath);
            foreach (string dir in Directory.GetDirectories(binDir))
            {
                foreach (string file in Directory.GetFiles(dir))
                {
                    string dest = Path.Combine(binDir, Path.GetFileName(file));
                    if (File.Exists(dest)) File.Delete(dest);
                    File.Move(file, dest);
                }
                Directory.Delete(dir, true);
            }
            Console.WriteLine("Extraction complete.");
        }

        string scriptDir = Directory.GetCurrentDirectory();
        CreateJunction(Path.Combine(scriptDir, "mods"), Path.Combine(baseDir, "mods"));
        CreateJunction(Path.Combine(scriptDir, "resourcepacks"), Path.Combine(baseDir, "resourcepacks"));

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

    static void CreateJunction(string source, string target)
    {
        if (!Directory.Exists(source))
            Directory.CreateDirectory(source);

        if (Directory.Exists(target))
        {
            Directory.Delete(target, true);
            Console.WriteLine(string.Format("Removed existing target: {0}", target));
        }

        Console.WriteLine(string.Format("Creating junction: {0} -> {1}", target, source));
        Process.Start("cmd", string.Format("/c mklink /J \"{0}\" \"{1}\"", target, source)).WaitForExit();

        if (Directory.Exists(target))
            Console.WriteLine("Junction created successfully.");
        else
            Console.WriteLine("Failed to create junction.");
    }
}