using AOC_SMS.UI.Data.Entities;
using Microsoft.EntityFrameworkCore;

namespace AOC_SMS.UI.Data;

public class AocSmsDbContext : DbContext
{
    public AocSmsDbContext(DbContextOptions<AocSmsDbContext> options)
        : base(options)
    {
    }

    public DbSet<RecipientEntity> Recipients => Set<RecipientEntity>();

    public DbSet<RecipientListEntity> RecipientLists => Set<RecipientListEntity>();

    public DbSet<RecipientListMemberEntity> RecipientListMembers => Set<RecipientListMemberEntity>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        base.OnModelCreating(modelBuilder);

        modelBuilder.Entity<RecipientEntity>(entity =>
        {
            entity.ToTable("Recipients");

            entity.HasKey(x => x.Id);

            entity.Property(x => x.FirstName)
                .IsRequired()
                .HasMaxLength(200);

            entity.Property(x => x.LastName)
                .IsRequired()
                .HasMaxLength(200);

            entity.Property(x => x.PhoneE164)
                .IsRequired()
                .HasMaxLength(32);

            entity.HasIndex(x => x.PhoneE164)
                .IsUnique();
        });

        modelBuilder.Entity<RecipientListEntity>(entity =>
        {
            entity.ToTable("RecipientLists");

            entity.HasKey(x => x.Id);

            entity.Property(x => x.Name)
                .IsRequired()
                .HasMaxLength(200);

            entity.Property(x => x.Type)
                .IsRequired()
                .HasMaxLength(32);

            entity.HasIndex(x => x.Name)
                .IsUnique();
        });

        modelBuilder.Entity<RecipientListMemberEntity>(entity =>
        {
            entity.ToTable("RecipientListMembers");

            entity.HasKey(x => new { x.RecipientListId, x.RecipientId });

            entity.HasOne(x => x.Recipient)
                .WithMany(x => x.ListMemberships)
                .HasForeignKey(x => x.RecipientId)
                .OnDelete(DeleteBehavior.Cascade);

            entity.HasOne(x => x.RecipientList)
                .WithMany(x => x.Members)
                .HasForeignKey(x => x.RecipientListId)
                .OnDelete(DeleteBehavior.Cascade);
        });
    }
}
