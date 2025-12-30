using System.ComponentModel.DataAnnotations;
using AOC_SMS;
using AOC_SMS.Models;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.RazorPages;
using Microsoft.AspNetCore.Mvc.Rendering;

namespace AOC_SMS.UI.Pages.Sms;

public class EventModel : PageModel
{
    private const string OptOutFooter = "Reply STOP to unsubscribe";
    private readonly EventSMSSender _smsSender;

    public EventModel(EventSMSSender smsSender)
    {
        _smsSender = smsSender;
    }

    [BindProperty]
    public InputModel Input { get; set; } = new();

    public List<SelectListItem> EventOptions { get; private set; } = new();

    public int RecipientCount { get; private set; }

    public bool ResultIsSuccess { get; private set; }

    public string? ResultTitle { get; private set; }

    public string? ResultMessage { get; private set; }

    public string SelectedEventDisplayName { get; private set; } = "";

    public List<SmsSendReceipt> Receipts { get; private set; } = new();

    public void OnGet(string? eventFile)
    {
        Input.IncludeOptOut = true;
        LoadEventOptions();

        if (EventOptions.Count > 0)
        {
            if (!string.IsNullOrWhiteSpace(eventFile) && IsValidEventSelection(eventFile))
            {
                Input.EventFile = eventFile;
            }
            else
            {
                Input.EventFile = EventOptions[0].Value ?? string.Empty;
            }

            RecipientCount = SafeGetRecipientCount(Input.EventFile);
            SelectedEventDisplayName = GetDisplayNameFromFile(Input.EventFile);
        }
    }

    public IActionResult OnGetEventInfo(string? eventFile)
    {
        Response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate";
        Response.Headers["Pragma"] = "no-cache";

        LoadEventOptions();

        if (!IsValidEventSelection(eventFile))
        {
            return new JsonResult(new
            {
                recipientCount = 0,
                displayName = string.Empty
            });
        }

        return new JsonResult(new
        {
            recipientCount = SafeGetRecipientCount(eventFile),
            displayName = GetDisplayNameFromFile(eventFile)
        });
    }

    public IActionResult OnPost()
    {
        LoadEventOptions();

        if (EventOptions.Count == 0)
        {
            ModelState.AddModelError(string.Empty, "No event files are available.");
            return Page();
        }

        if (!string.Equals(Input.Confirm?.Trim(), "SEND", StringComparison.OrdinalIgnoreCase))
        {
            ModelState.AddModelError("Input.Confirm", "Please type SEND to confirm.");
        }

        if (!IsValidEventSelection(Input.EventFile))
        {
            ModelState.AddModelError("Input.EventFile", "Please choose a valid event.");
        }

        RecipientCount = SafeGetRecipientCount(Input.EventFile);
        SelectedEventDisplayName = GetDisplayNameFromFile(Input.EventFile);

        if (!ModelState.IsValid)
        {
            return Page();
        }

        try
        {
            Receipts = _smsSender.SendSMSWithReceipts(BuildFinalMessage(Input.Message, Input.IncludeOptOut), Input.EventFile);

            ResultIsSuccess = true;
            ResultTitle = "Send started";
            ResultMessage = $"Your message was submitted for {RecipientCount} recipients.";
        }
        catch (Exception ex)
        {
            ResultIsSuccess = false;
            ResultTitle = "Send failed";
            ResultMessage = ex.Message;
        }

        return Page();
    }

    private void LoadEventOptions()
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

        var eventFiles = files
            .Select(Path.GetFileName)
            .Where(f => !string.IsNullOrWhiteSpace(f))
            .Where(f => !string.Equals(f, "AOC_Phone_Numbers.csv", StringComparison.OrdinalIgnoreCase))
            .OrderBy(f => f, StringComparer.OrdinalIgnoreCase)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToList();

        EventOptions = eventFiles
            .Select(f => new SelectListItem(GetDisplayNameFromFile(f!), f))
            .ToList();

        if (string.IsNullOrWhiteSpace(Input.EventFile) && EventOptions.Count > 0)
        {
            Input.EventFile = EventOptions[0].Value ?? string.Empty;
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

    private bool IsValidEventSelection(string? eventFile)
    {
        if (string.IsNullOrWhiteSpace(eventFile))
        {
            return false;
        }

        eventFile = Path.GetFileName(eventFile);
        if (!string.Equals(Path.GetFileName(eventFile), eventFile, StringComparison.Ordinal))
        {
            return false;
        }

        return EventOptions.Any(o => string.Equals(o.Value, eventFile, StringComparison.OrdinalIgnoreCase));
    }

    private static string BuildFinalMessage(string message, bool includeOptOut)
    {
        var trimmed = (message ?? string.Empty).Trim();
        if (!includeOptOut)
        {
            return trimmed;
        }

        return string.IsNullOrWhiteSpace(trimmed)
            ? OptOutFooter
            : $"{trimmed}\n\n{OptOutFooter}";
    }

    private int SafeGetRecipientCount(string? csvFileName)
    {
        try
        {
            if (string.IsNullOrWhiteSpace(csvFileName))
            {
                return 0;
            }

            return _smsSender.GetRecipients(csvFileName).Count;
        }
        catch
        {
            return 0;
        }
    }

    private static string GetDisplayNameFromFile(string? fileName)
    {
        if (string.IsNullOrWhiteSpace(fileName))
        {
            return string.Empty;
        }

        var name = Path.GetFileNameWithoutExtension(fileName);
        name = name.Replace("_Phone_Numbers", string.Empty, StringComparison.OrdinalIgnoreCase);
        name = name.Replace('_', ' ').Trim();
        return string.IsNullOrWhiteSpace(name) ? fileName : name;
    }

    public class InputModel
    {
        [Display(Name = "Event")]
        [Required(ErrorMessage = "Event is required.")]
        [StringLength(260)]
        public string EventFile { get; set; } = string.Empty;

        [Display(Name = "Message")]
        [Required(ErrorMessage = "Message is required.")]
        [StringLength(1000, ErrorMessage = "Message is too long.")]
        public string Message { get; set; } = string.Empty;

        [Display(Name = "Include opt-out footer")]
        public bool IncludeOptOut { get; set; }

        [Display(Name = "Confirmation")]
        [Required(ErrorMessage = "Confirmation is required.")]
        [StringLength(20)]
        public string Confirm { get; set; } = string.Empty;
    }
}
