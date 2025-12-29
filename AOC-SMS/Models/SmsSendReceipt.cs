using System;

namespace AOC_SMS.Models;

public class SmsSendReceipt
{
    public string? FirstName { get; set; }

    public string? LastName { get; set; }

    public string PhoneNumber { get; set; } = string.Empty;

    public string? MessageSid { get; set; }

    public string? Status { get; set; }

    public int? ErrorCode { get; set; }

    public string? ErrorMessage { get; set; }

    public bool Accepted => !string.IsNullOrWhiteSpace(MessageSid)
        && ErrorCode is null
        && !string.Equals(Status, "Failed", StringComparison.OrdinalIgnoreCase);
}
