using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.Build.Reporting;
using UnityEngine;

namespace DigitalTwin.Editor
{
    public static class WindowsPlayerBuild
    {
        private const string DefaultOutput =
            "Builds/Windows/DigitalTwin_Press.exe";

        public static void Build()
        {
            string output = GetCommandLineValue("-buildOutput");
            if (string.IsNullOrWhiteSpace(output))
                output = Path.GetFullPath(DefaultOutput);
            else
                output = Path.GetFullPath(output);

            string[] scenes = EditorBuildSettings.scenes
                .Where(scene => scene.enabled)
                .Select(scene => scene.path)
                .ToArray();
            if (scenes.Length == 0)
                throw new InvalidOperationException(
                    "Editor Build Settings에 활성화된 씬이 없습니다.");

            string outputDirectory = Path.GetDirectoryName(output);
            if (string.IsNullOrWhiteSpace(outputDirectory))
                throw new InvalidOperationException($"잘못된 빌드 출력 경로입니다: {output}");
            Directory.CreateDirectory(outputDirectory);

            Debug.Log(
                $"[Windows Build] 시작: output={output}, " +
                $"scenes={string.Join(", ", scenes)}");

            var options = new BuildPlayerOptions
            {
                scenes = scenes,
                locationPathName = output,
                target = BuildTarget.StandaloneWindows64,
                options = BuildOptions.StrictMode,
            };
            BuildReport report = BuildPipeline.BuildPlayer(options);
            BuildSummary summary = report.summary;

            Debug.Log(
                $"[Windows Build] 결과={summary.result}, " +
                $"크기={summary.totalSize:N0} bytes, " +
                $"시간={summary.totalTime}, " +
                $"경고={summary.totalWarnings}, 오류={summary.totalErrors}");

            if (summary.result != BuildResult.Succeeded)
            {
                throw new InvalidOperationException(
                    $"Windows 실행파일 빌드 실패: {summary.result}, " +
                    $"오류={summary.totalErrors}");
            }
        }

        private static string GetCommandLineValue(string name)
        {
            string[] arguments = Environment.GetCommandLineArgs();
            for (int index = 0; index < arguments.Length - 1; index++)
            {
                if (string.Equals(
                        arguments[index],
                        name,
                        StringComparison.OrdinalIgnoreCase))
                {
                    return arguments[index + 1];
                }
            }

            return string.Empty;
        }
    }
}
