# AOC-SMS

This project sends SMS messages to Armenians of Colorado (AOC) recipients using the Twilio API.

The app now includes a Razor Pages web UI (dashboard) with two sending modes:
- **Community SMS**: sends to all community members
- **Event SMS**: sends only to people registered for a specific event

Note: DO NOT SEND MORE THAN 2 SMS MESSAGES PER TESTING PHASE/TEXT

## Projects

- **AOC-SMS** (`AOC-SMS/AOC-SMS.csproj`)
  - Shared SMS-sending logic (class library)
  - Contains `SMSSender` and `EventSMSSender`
- **AOC-SMS.UI** (`AOC-SMS/AOC-SMS.UI/AOC-SMS.UI.csproj`)
  - ASP.NET Core Razor Pages web app (this is the main executable app)
  - Dashboard + send forms

## How to run

### Run the Web UI (recommended)

From the repo root:

```bash
dotnet run --project "./AOC-SMS/AOC-SMS.UI/AOC-SMS.UI.csproj"
```

If your current directory is already `./AOC-SMS`:

```bash
dotnet run --project "./AOC-SMS.UI/AOC-SMS.UI.csproj"
```

Then open the URL printed in the terminal (typically `http://localhost:5271`).

### Troubleshooting: "address already in use" (port 5271)

If you see an error like:

```
Failed to bind to address http://127.0.0.1:5271: address already in use.
```

It means the app is already running on that port.

- **Option A (recommended):** open `http://localhost:5271` in your browser.
- **Option B:** stop the previously running server (the terminal that started it) with `Ctrl+C`.
- **Option C:** run on a different port:

```bash
dotnet run --project "./AOC-SMS/AOC-SMS.UI/AOC-SMS.UI.csproj" --urls "http://localhost:5272"
```

### Build everything

```bash
dotnet build "./AOC-SMS/AOC-SMS.sln"
```

### What about the "actual app"?

The "actual app" is now the Web UI project (`AOC-SMS.UI`).

The old console entry point was moved aside to avoid conflicts with the web project:
- `AOC-SMS/Program.cs` was renamed to `AOC-SMS/Program.cs.bak`

If you want a console runner again, you can restore it (rename back to `Program.cs` and set the project output type back to an executable) or create a separate console project that references `AOC-SMS`.

## Data files (recipients)

Recipient lists are stored as CSV files under:

`AOC-SMS/App_Data/`

The Web UI automatically copies these CSVs into its build output so the app can read them at runtime.

### Community recipients

`AOC_Phone_Numbers.csv` format:

```
FirstName,LastName,PhoneNumber
```

Example:

```
Michael Humphrey,(323) 660-3202
```

### Event recipients

Event recipient CSVs are any other `*.csv` files in `App_Data` (excluding `AOC_Phone_Numbers.csv`).

Example:

`Gala_Phone_Numbers.csv` format:

```
720-345-2355
303-939-2939
...
```

These files show up in the **Event SMS** dropdown.

## Web UI pages

- Dashboard: `/`
- Community SMS: `/Sms/Community`
- Event SMS: `/Sms/Event`

Both send pages require typing **SEND** before the message is submitted.

1. Future Development:
- a.(DONE) Add a web interface to send SMS messages to a list of recipients using the Twilio API.
- b.Add a database to store the list of recipients.
- c.Add a scheduler to send SMS messages to the list of recipients at a specific time.

2. Web Interface:
- a.History of sent messages
- b.Feature to pull the list of attendees from event submission on WordPress and add to list of phone numbers
- c.Create WordPress plugin to send SMS messages to the list of recipients

3. Reporting of Delivery Status:
- a.Number of messages sent
- b.Number of messages delivered
- c.Number of messages failed to deliver
- d.Number of scheduled messages

4. Setup Security for API keys:
- a.Use environment variables to store API keys
- b.Use a secure method to store API keys
- c.Use a secure method to access API keys

5. Setup Security for Phone Number Data:
- a.Use environment variables to store phone number data
- b.Use a secure method to store phone number data
- c.Use a secure method to access phone number data
- d.Use a secure method to encrypt phone number data

