using System.ComponentModel.DataAnnotations;
using AOC_SMS;
using AOC_SMS.Models;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.RazorPages;

namespace AOC_SMS.UI.Pages.Sms;

public class CommunityModel : PageModel
{
    private const string OptOutFooter = "Reply STOP to unsubscribe";
    private readonly SMSSender _smsSender;

    public CommunityModel(SMSSender smsSender)
    {
        _smsSender = smsSender;
    }

    [BindProperty]
    public InputModel Input { get; set; } = new();

    public int RecipientCount { get; private set; }

    public bool ResultIsSuccess { get; private set; }

    public string? ResultTitle { get; private set; }

    public string? ResultMessage { get; private set; }

    public List<SmsSendReceipt> Receipts { get; private set; } = new();

    public IActionResult OnGetCommunityInfo()
    {
        Response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate";
        Response.Headers["Pragma"] = "no-cache";

        return new JsonResult(new
        {
            recipientCount = SafeGetRecipientCount()
        });
    }

    public void OnGet()
    {
        Input.IncludeOptOut = true;
        RecipientCount = SafeGetRecipientCount();
    }

    public IActionResult OnPost()
    {
        RecipientCount = SafeGetRecipientCount();

        if (!string.Equals(Input.Confirm?.Trim(), "SEND", StringComparison.OrdinalIgnoreCase))
        {
            ModelState.AddModelError("Input.Confirm", "Please type SEND to confirm.");
        }

        if (!ModelState.IsValid)
        {
            return Page();
        }

        try
        {
            Receipts = _smsSender.SendSMSWithReceipts(BuildFinalMessage(Input.Message, Input.IncludeOptOut));

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

    private int SafeGetRecipientCount()
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

    public class InputModel
    {
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
