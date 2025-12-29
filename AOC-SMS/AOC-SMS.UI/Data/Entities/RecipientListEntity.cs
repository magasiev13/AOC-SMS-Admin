namespace AOC_SMS.UI.Data.Entities;

public class RecipientListEntity
{
    public long Id { get; set; }

    public string Name { get; set; } = string.Empty;

    public string Type { get; set; } = string.Empty;

    public List<RecipientListMemberEntity> Members { get; set; } = new();
}
