using AOC_SMS.Models;
using Microsoft.Extensions.Options;
using System.Linq;
using Twilio;
using Twilio.Rest.Api.V2010.Account;
using Twilio.Types;

namespace AOC_SMS
{
    public class EventSMSSender
    {
        private readonly TwilioSettings _settings;

        public EventSMSSender(IOptions<TwilioSettings> options)
        {
            _settings = options.Value ?? new TwilioSettings();
        }

        public List<Recipient> GetRecipients() => GetRecipients("Gala_Phone_Numbers.csv");

        public List<Recipient> GetRecipients(string csvFileName)
        {
            var recipientList = new List<Recipient>();
            var csvPath = FindCsvPath(csvFileName);
            string[] lines = File.ReadAllLines(csvPath);
            foreach (string line in lines)
            {
                if (string.IsNullOrWhiteSpace(line))
                {
                    continue;
                }

                string[] columns = line.Split(',');

                string firstName = string.Empty;
                string lastName = string.Empty;
                string phone;
                if (columns.Length >= 3)
                {
                    firstName = columns[0].Trim();
                    lastName = columns[1].Trim();
                    phone = columns[2].Trim();
                }
                else if (columns.Length == 2)
                {
                    firstName = columns[0].Trim();
                    phone = columns[1].Trim();
                }
                else
                {
                    phone = columns[0].Trim();
                }

                if (string.IsNullOrWhiteSpace(phone))
                {
                    continue;
                }

                recipientList.Add(new Recipient
                {
                    FirstName = firstName,
                    LastName = lastName,
                    PhoneNumber = phone
                });
            }
            return recipientList;
        }

        //send a twilio sms message to each recipient
        public void SendSMS(string messageBody) => SendSMS(messageBody, "Gala_Phone_Numbers.csv");

        public void SendSMS(string messageBody, string csvFileName)
        {
            _ = SendSMSWithReceipts(messageBody, csvFileName);
        }

        public List<SmsSendReceipt> SendSMSWithReceipts(string messageBody) => SendSMSWithReceipts(messageBody, "Gala_Phone_Numbers.csv");

        public List<SmsSendReceipt> SendSMSWithReceipts(string messageBody, string csvFileName)
        {
            var recipients = GetRecipients(csvFileName);
            var receipts = recipients
                .Select(r => new SmsSendReceipt
                {
                    FirstName = r.FirstName,
                    LastName = r.LastName,
                    PhoneNumber = r.PhoneNumber
                })
                .ToList();

            try
            {
                EnsureTwilioConfigured(_settings);
                TwilioClient.Init(_settings.AccountSid, _settings.AuthToken);
            }
            catch (Exception ex)
            {
                foreach (var receipt in receipts)
                {
                    receipt.Status = "Failed";
                    receipt.ErrorMessage = ex.Message;
                }

                return receipts;
            }

            foreach (var receipt in receipts)
            {
                try
                {
                    var messageOptions = new CreateMessageOptions(new PhoneNumber(receipt.PhoneNumber))
                    {
                        MessagingServiceSid = _settings.MessagingServiceSid,
                        Body = messageBody
                    };

                    var message = MessageResource.Create(messageOptions);
                    receipt.MessageSid = message.Sid;
                    receipt.Status = message.Status?.ToString();
                    receipt.ErrorCode = message.ErrorCode;
                    receipt.ErrorMessage = message.ErrorMessage;

                    Console.WriteLine($"{receipt.PhoneNumber}: {receipt.Status}");
                }
                catch (Exception ex)
                {
                    receipt.Status = "Failed";
                    receipt.ErrorMessage = ex.Message;
                    Console.WriteLine($"{receipt.PhoneNumber}: Failed ({ex.Message})");
                }
            }

            return receipts;
        }

        private static string FindCsvPath(string csvFileName)
        {
            var file = Path.GetFileName(csvFileName);

            var candidates = new[]
            {
                Path.Combine(Directory.GetCurrentDirectory(), "App_Data", file),
                Path.GetFullPath(Path.Combine(Directory.GetCurrentDirectory(), "AOC-SMS", "App_Data", file)),
                Path.GetFullPath(Path.Combine(Directory.GetCurrentDirectory(), "..", "App_Data", file))
                ,Path.GetFullPath(Path.Combine(Directory.GetCurrentDirectory(), "..", "AOC-SMS", "App_Data", file))
                ,Path.Combine(AppContext.BaseDirectory, "App_Data", file)
            };

            foreach (var candidate in candidates)
            {
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }

            return candidates[0];
        }

        private static void EnsureTwilioConfigured(TwilioSettings settings)
        {
            if (string.IsNullOrWhiteSpace(settings.AccountSid)
                || string.IsNullOrWhiteSpace(settings.AuthToken)
                || string.IsNullOrWhiteSpace(settings.MessagingServiceSid))
            {
                throw new InvalidOperationException(
                    "Twilio configuration is missing. Set Twilio:AccountSid, Twilio:AuthToken, and Twilio:MessagingServiceSid.");
            }
        }
    }
}
