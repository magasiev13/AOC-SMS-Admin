// See https://aka.ms/new-console-template for more information
using AOC_SMS;

var message = @"AOC is excited to start a text message communication service! We will use this channel for important notifications only, ensuring you don’t miss any new events or announcements.

Thank you for being part of our community!

Best regards,
AOC

Reply STOP to unsubscribe";

SMSSender smsSender = new SMSSender();
smsSender.SendSMS(message);
