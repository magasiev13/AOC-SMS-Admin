namespace AOC_SMS.UI.Data.Entities;

public class RecipientListMemberEntity
{
    public long RecipientListId { get; set; }

    public long RecipientId { get; set; }

    public RecipientListEntity RecipientList { get; set; } = null!;

    public RecipientEntity Recipient { get; set; } = null!;
}
