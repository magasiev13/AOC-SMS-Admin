using Microsoft.EntityFrameworkCore.Migrations;
using Npgsql.EntityFrameworkCore.PostgreSQL.Metadata;

#nullable disable

namespace AOC_SMS.UI.Migrations
{
    /// <inheritdoc />
    public partial class InitialRecipients : Migration
    {
        /// <inheritdoc />
        protected override void Up(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.CreateTable(
                name: "RecipientLists",
                columns: table => new
                {
                    Id = table.Column<long>(type: "bigint", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    Name = table.Column<string>(type: "character varying(200)", maxLength: 200, nullable: false),
                    Type = table.Column<string>(type: "character varying(32)", maxLength: 32, nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_RecipientLists", x => x.Id);
                });

            migrationBuilder.CreateTable(
                name: "Recipients",
                columns: table => new
                {
                    Id = table.Column<long>(type: "bigint", nullable: false)
                        .Annotation("Npgsql:ValueGenerationStrategy", NpgsqlValueGenerationStrategy.IdentityByDefaultColumn),
                    FirstName = table.Column<string>(type: "character varying(200)", maxLength: 200, nullable: false),
                    LastName = table.Column<string>(type: "character varying(200)", maxLength: 200, nullable: false),
                    PhoneE164 = table.Column<string>(type: "character varying(32)", maxLength: 32, nullable: false),
                    IsOptedOut = table.Column<bool>(type: "boolean", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_Recipients", x => x.Id);
                });

            migrationBuilder.CreateTable(
                name: "RecipientListMembers",
                columns: table => new
                {
                    RecipientListId = table.Column<long>(type: "bigint", nullable: false),
                    RecipientId = table.Column<long>(type: "bigint", nullable: false)
                },
                constraints: table =>
                {
                    table.PrimaryKey("PK_RecipientListMembers", x => new { x.RecipientListId, x.RecipientId });
                    table.ForeignKey(
                        name: "FK_RecipientListMembers_RecipientLists_RecipientListId",
                        column: x => x.RecipientListId,
                        principalTable: "RecipientLists",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                    table.ForeignKey(
                        name: "FK_RecipientListMembers_Recipients_RecipientId",
                        column: x => x.RecipientId,
                        principalTable: "Recipients",
                        principalColumn: "Id",
                        onDelete: ReferentialAction.Cascade);
                });

            migrationBuilder.CreateIndex(
                name: "IX_RecipientListMembers_RecipientId",
                table: "RecipientListMembers",
                column: "RecipientId");

            migrationBuilder.CreateIndex(
                name: "IX_RecipientLists_Name",
                table: "RecipientLists",
                column: "Name",
                unique: true);

            migrationBuilder.CreateIndex(
                name: "IX_Recipients_PhoneE164",
                table: "Recipients",
                column: "PhoneE164",
                unique: true);
        }

        /// <inheritdoc />
        protected override void Down(MigrationBuilder migrationBuilder)
        {
            migrationBuilder.DropTable(
                name: "RecipientListMembers");

            migrationBuilder.DropTable(
                name: "RecipientLists");

            migrationBuilder.DropTable(
                name: "Recipients");
        }
    }
}
