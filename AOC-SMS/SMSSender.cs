using AOC_SMS.Models;
using System.Linq;
using Twilio;
using Twilio.Rest.Api.V2010.Account;
using Twilio.Types;

namespace AOC_SMS
{
    public class SMSSender
    {
        public SMSSender()
        {
            
        }

        //read the csv file from the App_Data folder
        public List<Recipient> GetRecipients()
        {
            var recipientList = new List<Recipient>();
            var csvPath = FindCsvPath("AOC_Phone_Numbers.csv");
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

        //send a twilio sms message to each recipient
        public void SendSMS(string messageBody)
        {
            _ = SendSMSWithReceipts(messageBody);
        }

        public List<SmsSendReceipt> SendSMSWithReceipts(string messageBody)
        {
            var recipients = GetRecipients();
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
                var accountSid = "AC8a22ae65eb76f569be174c31c2255b6f";
                var authToken = "fd9bf45f27b66e9a985d72aa1f390e55";
                TwilioClient.Init(accountSid, authToken);
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
                        MessagingServiceSid = "MGabf6500f6dbb920fe2b8a42221996a7f",
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
    }
}
