using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using AOC_SMS.Models;
using System.IO;
using System;
using System.Collections.Generic;
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
            string[] lines = File.ReadAllLines(@"C:\Users\Vartan\source\repos\vardanho\AOC-SMS\AOC-SMS\App_Data\AOC_Phone_Numbers.csv");
            foreach (string line in lines)
            {
                string[] columns = line.Split(',');
                recipientList.Add(new Recipient
                {
                    FirstName = columns[0],
                    LastName = columns[1],
                    PhoneNumber = columns[2]
                });
            }
            return recipientList;
        }

        //send a twilio sms message to each recipient
        public void SendSMS(string messageBody)
        {
            var recipients = GetRecipients();
            foreach (var recipient in recipients)
            {
                var accountSid = "AC8a22ae65eb76f569be174c31c2255b6f";
                var authToken = "fd9bf45f27b66e9a985d72aa1f390e55";
                TwilioClient.Init(accountSid, authToken);

                var messageOptions = new CreateMessageOptions(
                  new PhoneNumber(recipient.PhoneNumber));
                messageOptions.MessagingServiceSid = "MGabf6500f6dbb920fe2b8a42221996a7f";
                messageOptions.Body = messageBody;


                var message = MessageResource.Create(messageOptions);
                Console.WriteLine($"{recipient.PhoneNumber}: {message.Status}");
            }
        }
    }
}
