// See https://aka.ms/new-console-template for more information
using AOC_SMS;

var message = @"Just a reminder that Comedy Night with Ashot Ghazaryan is only 3 days away! Tickets are almost sold out, so make sure to buy yours before the sale ends! For tickets, visit https://armeniansofcolorado.org/events/ashot-ghazaryan/

Looking forward to seeing you there!

Best regards,
AOC

Reply STOP to unsubscribe";

SMSSender smsSender = new SMSSender();
smsSender.SendSMS(message);
