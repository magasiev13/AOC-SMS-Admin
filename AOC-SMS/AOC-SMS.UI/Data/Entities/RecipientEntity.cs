namespace AOC_SMS.UI.Data.Entities;

public class RecipientEntity
{
    public long Id { get; set; }

    public string FirstName { get; set; } = string.Empty;

    public string LastName { get; set; } = string.Empty;

    public string PhoneE164 { get; set; } = string.Empty;

    public bool IsOptedOut { get; set; }

    public List<RecipientListMemberEntity> ListMemberships { get; set; } = new();
}
