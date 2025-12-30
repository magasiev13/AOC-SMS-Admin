using System.Linq;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.RazorPages;
using AOC_SMS;

namespace AOC_SMS.UI.Pages;

public class IndexModel : PageModel
{
    private readonly SMSSender _smsSender;

    public IndexModel(SMSSender smsSender)
    {
        _smsSender = smsSender;
    }

    public int CommunityRecipientCount { get; private set; }

    public int EventFileCount { get; private set; }

    public void OnGet()
    {
        CommunityRecipientCount = SafeGetCommunityCount();
        EventFileCount = SafeGetEventFileCount();
    }

    public IActionResult OnGetDashboardInfo()
    {
        Response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate";
        Response.Headers["Pragma"] = "no-cache";

        return new JsonResult(new
        {
            communityRecipientCount = SafeGetCommunityCount(),
            eventFileCount = SafeGetEventFileCount()
        });
    }

    private int SafeGetCommunityCount()
    {
        try
        {
            return _smsSender.GetRecipients().Count;
        }
        catch
        {
            return 0;
        }
    }

    private static int SafeGetEventFileCount()
    {
        try
        {
            var files = new List<string>();
            foreach (var dir in GetPreferredAppDataDirectories())
            {
                try
                {
                    if (Directory.Exists(dir))
                    {
                        files.AddRange(Directory.GetFiles(dir, "*.csv"));
                    }
                }
                catch
                {
                }
            }

            return files
                .Select(Path.GetFileName)
                .Where(f => !string.IsNullOrWhiteSpace(f))
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .Count(f => !string.Equals(f, "AOC_Phone_Numbers.csv", StringComparison.OrdinalIgnoreCase));
        }
        catch
        {
            return 0;
        }
    }

    private static IReadOnlyList<string> GetPreferredAppDataDirectories()
    {
        var current = Directory.GetCurrentDirectory();

        var sourceCandidates = new List<string>
        {
            Path.Combine(current, "App_Data"),
            Path.Combine(current, "AOC-SMS", "App_Data"),
            Path.GetFullPath(Path.Combine(current, "..", "App_Data")),
            Path.GetFullPath(Path.Combine(current, "..", "AOC-SMS", "App_Data"))
        };

        var existingSourceDirs = sourceCandidates
            .Where(Directory.Exists)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToList();

        if (existingSourceDirs.Count > 0)
        {
            return existingSourceDirs;
        }

        var outputDir = Path.Combine(AppContext.BaseDirectory, "App_Data");
        return Directory.Exists(outputDir)
            ? new List<string> { outputDir }
            : new List<string>();
    }
}
