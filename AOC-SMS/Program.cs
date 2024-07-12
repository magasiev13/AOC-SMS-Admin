// See https://aka.ms/new-console-template for more information
using AOC_SMS;

var message = @"The biggest AOC event of the year, annual Picnic, is around the corner.  

For more information and to buy tickets, please visit https://armeniansofcolorado.org/events/picnic-2024/

We look forward to seeing you there!

Best regards,
AOC

Reply STOP to unsubscribe";

SMSSender smsSender = new SMSSender();
smsSender.SendSMS(message);
